"""Edge tunnel: the device side of the Herald edge relay.

Holds ONE outbound WebSocket to the edge (a Cloudflare Durable Object per
device) and answers relayed HTTP requests against the local Hermes
api_server. Compared with the SSE tunnel (relay_client.py) it:

* costs the edge nothing while idle — the edge object hibernates and our
  keepalive ``ping`` is answered by the runtime without waking it;
* carries a request's response back on the same socket, chunk by chunk, so a
  streaming run is one frame per SSE line instead of one HTTPS POST per line;
* cancels the local read when the caller hangs up, while the Hermes run itself
  keeps going and can be resumed with ``Last-Event-ID``.

Wire protocol: see edge/src/index.js.
"""
from __future__ import annotations

import asyncio
import codecs
import json
import logging
import random

import aiohttp

logger = logging.getLogger("herald.edge")

# Keepalive = WebSocket PROTOCOL ping frames (RFC 6455 §5.5.2), not text
# messages. Cloudflare does not bill incoming protocol pings and they never wake
# a hibernated Durable Object, so an idle device costs nothing. 50 s stays under
# Cloudflare's 100 s idle cutoff and typical NAT/carrier timeouts (2-5 min).
# aiohttp closes the socket if a pong is late, which triggers reconnect.
PING_INTERVAL_S = 50.0
BACKOFF_MAX_S = 60.0
# Only these headers reach local Hermes (mirrors the edge's allowlist).
_FORWARD = ("authorization", "accept", "content-type", "last-event-id")


class EdgeTunnel:
    def __init__(self, edge_url: str, device_token: str, hermes_url: str,
                 hermes_key: str = "") -> None:
        self.edge_url = edge_url.rstrip("/")
        self.device_token = device_token
        self.hermes_url = hermes_url.rstrip("/")
        self.hermes_key = hermes_key
        self.on_event = None  # optional async callback(dict) for server pushes
        self._inflight: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self.connected = asyncio.Event()

    # ------------------------------------------------------------------ public
    async def run_forever(self) -> None:
        attempt = 0
        async with aiohttp.ClientSession() as http:
            self._http = http
            while not self._stop.is_set():
                try:
                    await self._session(http)
                    attempt = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.warning("edge tunnel dropped: %s", exc)
                self.connected.clear()
                if self._stop.is_set():
                    break
                attempt += 1
                # Full jitter: after an edge deploy every device reconnects at
                # once; spreading them avoids a thundering herd.
                delay = random.uniform(0, min(BACKOFF_MAX_S, 2 ** min(attempt, 6)))
                await asyncio.sleep(delay)

    async def stop(self) -> None:
        self._stop.set()
        for t in list(self._inflight.values()):
            t.cancel()

    # --------------------------------------------------------------- internals
    async def _session(self, http: aiohttp.ClientSession) -> None:
        url = self.edge_url.replace("https://", "wss://").replace("http://", "ws://")
        async with http.ws_connect(
            f"{url}/agent/connect",
            headers={"Authorization": f"Bearer {self.device_token}"},
            heartbeat=PING_INTERVAL_S,
            max_msg_size=0,
        ) as ws:
            logger.info("edge tunnel connected: %s", self.edge_url)
            self.connected.set()
            self._ws = ws
            try:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        self._dispatch(ws, msg.data)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
            finally:
                for t in list(self._inflight.values()):
                    t.cancel()
                self._inflight.clear()

    def _dispatch(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        kind = msg.get("t")
        if kind == "req":
            rid = msg.get("id", "")
            task = asyncio.create_task(self._serve(ws, msg))
            self._inflight[rid] = task
            task.add_done_callback(lambda _t, r=rid: self._inflight.pop(r, None))
        elif kind == "cancel":
            task = self._inflight.get(msg.get("id", ""))
            if task:
                task.cancel()
        elif kind == "event" and self.on_event:
            asyncio.create_task(self.on_event(msg.get("data")))

    def _headers(self, forwarded: dict) -> dict:
        out = {k: v for k, v in (forwarded or {}).items() if k.lower() in _FORWARD}
        # The caller's own key wins (per-profile keys on a multiplexed gateway);
        # fall back to this machine's key for callers that send none.
        if "authorization" not in {k.lower() for k in out} and self.hermes_key:
            out["Authorization"] = f"Bearer {self.hermes_key}"
        return out

    async def _serve(self, ws, msg: dict) -> None:
        rid = msg.get("id", "")
        method = (msg.get("method") or "GET").upper()
        path = msg.get("path") or "/"
        body = msg.get("body")

        async def send(obj: dict) -> None:
            if not ws.closed:
                await ws.send_str(json.dumps(obj))

        try:
            async with self._http.request(
                method,
                f"{self.hermes_url}{path}",
                data=body.encode() if isinstance(body, str) else None,
                headers=self._headers(msg.get("headers") or {}),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=None),
            ) as resp:
                ctype = resp.headers.get("content-type", "")
                await send({"t": "res", "id": rid, "status": resp.status,
                            "headers": {"content-type": ctype} if ctype else {}})
                # Incremental decode: a network read can split a multi-byte
                # UTF-8 character; decoding pieces independently corrupts it.
                dec = codecs.getincrementaldecoder("utf-8")("replace")
                async for piece in resp.content.iter_any():
                    text = dec.decode(piece)
                    if text:
                        await send({"t": "chunk", "id": rid, "data": text})
                tail = dec.decode(b"", final=True)
                if tail:
                    await send({"t": "chunk", "id": rid, "data": tail})
            await send({"t": "end", "id": rid})
        except asyncio.CancelledError:
            logger.info("edge: caller hung up on %s %s (run continues)", method, path)
            raise
        except Exception as exc:  # noqa: BLE001
            await send({"t": "err", "id": rid, "error": f"{type(exc).__name__}: {exc}"})
