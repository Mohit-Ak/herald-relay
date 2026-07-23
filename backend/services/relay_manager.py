"""
Herald Relay – central relay manager.

Maintains the registry of connected Hermes plugin WebSocket connections and
provides the coroutine-based forwarding layer that the HTTP proxy endpoints
use to tunnel requests through the relay tunnel.

Race-condition note
-------------------
A forward_response (or sse_chunk/sse_done) message can theoretically arrive
from the plugin *before* the caller of forward_request has had a chance to
insert its asyncio.Queue into _pending_requests.  We guard against this by
keeping a secondary "orphan" buffer (_orphan_msgs) keyed by request_id.  Any
handler that finds no pending queue places the message there; forward_request
drains the buffer immediately after registering the queue.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections import defaultdict
from typing import AsyncGenerator

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class RelayManager:
    """Central registry for relay connections and in-flight HTTP requests."""

    def __init__(self) -> None:
        # device_token → active WebSocket
        self._connections: dict[str, WebSocket] = {}
        # request_id → Queue that receives response / SSE chunks
        self._pending_requests: dict[str, asyncio.Queue] = {}
        # Buffer for messages that arrived before the queue was created
        self._orphan_msgs: dict[str, list[dict]] = defaultdict(list)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def register(self, device_token: str, ws: WebSocket) -> str:
        """Register a newly-connected Hermes plugin WebSocket.

        Returns the relay_id (a fresh UUID) that the plugin should echo back.
        """
        relay_id = str(uuid.uuid4())
        self._connections[device_token] = ws
        logger.info("Registered device %s  relay_id=%s", device_token, relay_id)
        return relay_id

    async def unregister(self, device_token: str) -> None:
        """Remove a device from the registry (called on disconnect)."""
        self._connections.pop(device_token, None)
        logger.info("Unregistered device %s", device_token)

    def is_connected(self, device_token: str) -> bool:
        return device_token in self._connections

    def connected_count(self) -> int:
        return len(self._connections)

    # ------------------------------------------------------------------
    # Request forwarding
    # ------------------------------------------------------------------

    async def forward_request(
        self,
        device_token: str,
        method: str,
        path: str,
        body: dict | None,
        headers: dict,
        timeout: float = 60.0,
    ) -> AsyncGenerator[dict, None]:
        """Forward an HTTP request through the WS tunnel and yield response items.

        Yields dicts:
          {"type": "response",   "status": int, "body": dict, "headers": dict}
          {"type": "sse_chunk",  "data": str}
          {"type": "sse_done"}
          {"type": "error",      "message": str}
        """
        if not self.is_connected(device_token):
            raise ValueError(f"Device {device_token!r} is not connected")

        request_id = str(uuid.uuid4())
        queue: asyncio.Queue[dict] = asyncio.Queue()

        # Register queue *before* sending to avoid losing a very fast reply.
        self._pending_requests[request_id] = queue

        # Drain any orphan messages that arrived between send and queue creation
        # (shouldn't happen now, but belt-and-suspenders).
        for orphan in self._orphan_msgs.pop(request_id, []):
            await queue.put(orphan)

        ws = self._connections[device_token]
        envelope = {
            "type": "forward_request",
            "request_id": request_id,
            "method": method,
            "path": path,
            "body": body,
            "headers": headers,
        }
        try:
            await ws.send_text(json.dumps(envelope))
        except Exception as exc:
            self._pending_requests.pop(request_id, None)
            raise RuntimeError(f"Failed to send request to device: {exc}") from exc

        # Yield items from the queue until we get a terminal message.
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    yield {"type": "error", "message": "Request timed out"}
                    return
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=remaining)
                except asyncio.TimeoutError:
                    yield {"type": "error", "message": "Request timed out"}
                    return

                yield item

                # Terminal messages
                if item["type"] in ("response", "sse_done", "error"):
                    return
        finally:
            self._pending_requests.pop(request_id, None)

    # ------------------------------------------------------------------
    # Handlers called by the WS receive loop (routers/relay.py)
    # ------------------------------------------------------------------

    async def handle_forward_response(self, request_id: str, msg: dict) -> None:
        """Called when the plugin sends a complete (non-SSE) response."""
        item = {
            "type": "response",
            "status": msg.get("status", 200),
            "body": msg.get("body", {}),
            "headers": msg.get("headers", {}),
        }
        await self._deliver(request_id, item)

    async def handle_sse_chunk(self, request_id: str, data: str) -> None:
        await self._deliver(request_id, {"type": "sse_chunk", "data": data})

    async def handle_sse_done(self, request_id: str) -> None:
        await self._deliver(request_id, {"type": "sse_done"})

    async def _deliver(self, request_id: str, item: dict) -> None:
        """Put an item into the pending queue, or buffer it if queue not ready."""
        queue = self._pending_requests.get(request_id)
        if queue is not None:
            await queue.put(item)
        else:
            # Race: queue not yet created – buffer for forward_request to pick up.
            logger.debug(
                "Buffering orphan message for request_id=%s type=%s",
                request_id,
                item.get("type"),
            )
            self._orphan_msgs[request_id].append(item)

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------

    async def trigger_push(
        self,
        device_token: str,
        message: str,
        urgency: str,
        metadata: dict,
    ) -> None:
        """Delegate to the push router's send logic via an internal HTTP call.

        We import lazily to avoid circular imports.
        """
        try:
            from routers.push import send_push_notification  # type: ignore[import]

            await send_push_notification(device_token, message, urgency, metadata)
        except Exception as exc:
            logger.warning("trigger_push failed for %s: %s", device_token, exc)


# Module-level singleton – import this everywhere.
relay_manager = RelayManager()
