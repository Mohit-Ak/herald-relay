"""Client for the Herald edge relay (Cloudflare, one hibernating object per device).

WHY THIS EXISTS
---------------
Devices are moving off the long-lived SSE tunnel onto the edge relay, where an
idle connection costs nothing. Phones and the Herald backend still address a
device through this relay (``/hermes/d/<device_token>/**``), so no app release
is needed: when a device has no tunnel HERE, the request is passed to the edge,
which reaches the device over its own socket.

Configured by ``HERALD_EDGE_URL`` (+ ``HERALD_EDGE_SERVICE_KEY`` for server
pushes). Unset = edge disabled, behaviour identical to before.
"""
from __future__ import annotations

import logging
import os
import time
from typing import AsyncIterator, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger(__name__)

# Online checks are on the hot path of every proxied call; a short cache keeps
# a burst (health + soul + toolsets at call start) to one edge round trip.
_STATUS_TTL_S = 5.0
_status_cache: dict[str, tuple[float, bool]] = {}
_client: Optional[httpx.AsyncClient] = None


def edge_url() -> str:
    return os.getenv("HERALD_EDGE_URL", "").rstrip("/")


def enabled() -> bool:
    return bool(edge_url())


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        # read=None: run event streams last as long as the run.
        _client = httpx.AsyncClient(timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=5.0))
    return _client


def _device_base(token: str) -> str:
    return f"{edge_url()}/d/{quote(token, safe='')}"


async def is_online(token: str) -> bool:
    if not enabled() or not token:
        return False
    now = time.monotonic()
    hit = _status_cache.get(token)
    if hit and hit[0] > now:
        return hit[1]
    try:
        r = await _http().get(f"{_device_base(token)}/_edge/status", timeout=3.0)
        online = r.status_code == 200 and bool(r.json().get("online"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[edge] status check failed: %s", exc)
        online = False
    if len(_status_cache) > 10_000:
        _status_cache.clear()
    _status_cache[token] = (now + _STATUS_TTL_S, online)
    return online


def forget(token: str) -> None:
    _status_cache.pop(token, None)


def _content(body) -> Optional[bytes]:
    if body is None:
        return None
    import json  # noqa: PLC0415
    return body if isinstance(body, (bytes, bytearray)) else json.dumps(body).encode()


async def request(token: str, method: str, path: str, body=None,
                  headers: Optional[dict] = None) -> tuple[int, bytes, str]:
    """One buffered round trip. Returns (status, body_bytes, content_type)."""
    h = dict(headers or {})
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    r = await _http().request(method, f"{_device_base(token)}{path}", content=_content(body),
                              headers=h, timeout=httpx.Timeout(60.0, connect=5.0))
    if r.status_code == 503:
        forget(token)
    return r.status_code, r.content, r.headers.get("content-type", "application/json")


async def stream(token: str, method: str, path: str, body=None,
                 headers: Optional[dict] = None) -> AsyncIterator[str]:
    """Pass a device's streaming response through verbatim, as it arrives."""
    h = dict(headers or {})
    h.setdefault("Accept", "text/event-stream")
    if body is not None:
        h.setdefault("Content-Type", "application/json")
    async with _http().stream(method, f"{_device_base(token)}{path}",
                              content=_content(body), headers=h) as r:
        if r.status_code >= 400:
            forget(token)
            detail = (await r.aread())[:300].decode("utf-8", "replace")
            import json  # noqa: PLC0415
            yield f"data: {json.dumps({'error': 'edge_' + str(r.status_code), 'detail': detail})}\n\n"
            return
        async for text in r.aiter_text():
            if text:
                yield text


async def deliver(token: str, payload: dict) -> bool:
    """Push a server event (task / approval) to the device over its edge socket."""
    key = os.getenv("HERALD_EDGE_SERVICE_KEY", "")
    if not enabled() or not key:
        return False
    try:
        r = await _http().post(f"{edge_url()}/deliver/{quote(token, safe='')}", json=payload,
                               headers={"Authorization": f"Bearer {key}"}, timeout=10.0)
        return r.status_code == 200 and bool(r.json().get("delivered"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("[edge] deliver failed: %s", exc)
        return False
