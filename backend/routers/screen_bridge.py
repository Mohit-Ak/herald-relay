"""Screen sharing for an agent on the user's own computer: a byte pipe.

The Herald app shows a local agent's screen (Hermes' Bot Screen, a VNC
desktop) the same way it shows a cloud agent's. The computer sits behind NAT,
so the tunnel client on it dials OUT to us once per viewing and we join that
socket to the phone's:

  computer  ──ws──▶  /tunnel/screen/{sid}                (Bearer device token)
  phone     ──ws──▶  /hermes/d/{device_token}/_screen/{sid}

`sid` is made by the tunnel client AFTER it has checked the caller's agent key
(the phone asks it over the ordinary keyed proxy path), is random, lives 60
seconds and joins exactly one phone. We never judge the agent key here and we
never look inside the stream: who may send mouse and keyboard is enforced on
the computer, where the screen is.

Nothing here logs a token or a sid.
"""
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

ATTACH_TIMEOUT_S = 60.0          # how long a computer's socket waits for its phone
MAX_SESSION_S = 60 * 60.0        # Cloud Run ends a request at 3600 s anyway
MAX_WAITING_PER_DEVICE = 4
_SID = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{16,256}$")

# RFC 6455 close codes in the private range, read by the app and the plugin.
CLOSE_BAD_REQUEST = 4400
CLOSE_UNKNOWN = 4404         # no such viewing (never made, already used, expired)
CLOSE_NO_PHONE = 4408        # the phone never came
CLOSE_BUSY = 4429
CLOSE_PHONE_DROPPED = 4410   # the phone's link broke (not: the user closed the screen)


@dataclass
class _Waiting:
    agent: WebSocket
    token: str
    made: float = field(default_factory=time.monotonic)
    attached: asyncio.Event = field(default_factory=asyncio.Event)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # How the computer's socket is closed. It tells the computer whether the
    # user left on purpose (give control back now) or the phone's link broke
    # (keep the agent out for a moment: the user may be mid sign-in).
    close_code: int = 1000


_waiting: dict[str, _Waiting] = {}   # sid -> the computer's socket, until a phone takes it


def _bearer(ws: WebSocket) -> str:
    value = (ws.headers.get("authorization") or "").strip()
    return value[6:].strip() if value.lower().startswith("bearer") else ""


async def _close(ws: WebSocket, code: int, reason: str = "") -> None:
    try:
        await ws.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001  already closed by the peer
        pass


@router.websocket("/tunnel/screen/{sid}")
async def agent_side(ws: WebSocket, sid: str):
    """The computer's end. Waits for its phone, then stays open while the
    phone's handler moves the bytes."""
    token = _bearer(ws)
    await ws.accept()
    if not _SID.match(sid) or not _TOKEN.match(token) or sid in _waiting:
        await _close(ws, CLOSE_BAD_REQUEST, "bad request")
        return
    if sum(1 for w in _waiting.values() if w.token == token) >= MAX_WAITING_PER_DEVICE:
        await _close(ws, CLOSE_BUSY, "too many")
        return
    slot = _Waiting(agent=ws, token=token)
    _waiting[sid] = slot
    try:
        try:
            await asyncio.wait_for(slot.attached.wait(), ATTACH_TIMEOUT_S)
        except asyncio.TimeoutError:
            await _close(ws, CLOSE_NO_PHONE, "no viewer")
            return
        await slot.done.wait()
    finally:
        _waiting.pop(sid, None)
        slot.done.set()
        await _close(ws, slot.close_code)


@router.websocket("/hermes/d/{device_token}/_screen/{sid}")
async def phone_side(ws: WebSocket, device_token: str, sid: str):
    """The phone's end (noVNC in the app). Takes the waiting socket for this
    sid, once, and pipes the two together until either side leaves."""
    # noVNC asks for the "binary" subprotocol; a browser drops the socket if
    # the server doesn't echo one it offered.
    offered = [p.strip() for p in (ws.headers.get("sec-websocket-protocol") or "").split(",") if p.strip()]
    await ws.accept(subprotocol="binary" if "binary" in offered else None)
    slot = _waiting.get(sid) if _SID.match(sid) else None
    if slot is None or slot.attached.is_set() or not hmac.compare_digest(slot.token, device_token):
        await _close(ws, CLOSE_UNKNOWN, "unknown")
        return
    _waiting.pop(sid, None)          # single use
    slot.attached.set()
    started = time.monotonic()
    moved = {"up": 0, "down": 0}

    async def pump(src: WebSocket, dst: WebSocket, key: str) -> None:
        while True:
            msg = await src.receive()
            if msg.get("type") == "websocket.disconnect":
                if key == "up" and msg.get("code") not in (1000, 1001):
                    slot.close_code = CLOSE_PHONE_DROPPED
                return
            data = msg.get("bytes")
            if data:
                moved[key] += len(data)
                await dst.send_bytes(data)

    tasks = [asyncio.create_task(pump(slot.agent, ws, "down")),
             asyncio.create_task(pump(ws, slot.agent, "up"))]
    try:
        _done, pending = await asyncio.wait(tasks, timeout=MAX_SESSION_S,
                                            return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    except WebSocketDisconnect:
        pass
    finally:
        slot.done.set()
        await _close(ws, 1000)
        logger.info("[screen] viewing ended after %.0fs (%d bytes to phone, %d from it)",
                    time.monotonic() - started, moved["down"], moved["up"])
