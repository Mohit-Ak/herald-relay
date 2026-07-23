"""Herald Relay — outbound WebSocket client that tunnels Herald Cloud → local Hermes."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx
import websockets
import websockets.asyncio.client as ws_asyncio
from websockets.exceptions import ConnectionClosed, WebSocketException

# websockets 13+ uses ClientConnection; fall back to a generic type for annotations
try:
    from websockets.asyncio.client import ClientConnection as _WSConn
except ImportError:  # pragma: no cover
    _WSConn = object  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# SSE path patterns
_SSE_POST_PATH = "/v1/runs"
_SSE_EVENTS_RE = re.compile(r"^/v1/runs/[^/]+/events$")


def _is_sse_path(method: str, path: str) -> bool:
    """Return True if this request will produce an SSE stream."""
    if method.upper() == "POST" and path.rstrip("/") == _SSE_POST_PATH:
        return True
    if method.upper() == "GET" and _SSE_EVENTS_RE.match(path):
        return True
    return False


class HeraldRelayClient:
    """Persistent WebSocket client that relays Herald Cloud requests to local Hermes."""

    def __init__(self, relay_url: str, device_token: str, local_hermes_url: str):
        self.relay_url = relay_url
        self.device_token = device_token
        self.local_hermes_url = local_hermes_url.rstrip("/")

        self._running = False
        self._ws: Any = None
        self._send_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def run_forever(self) -> None:
        """Main loop: connect, handle messages, reconnect on disconnect."""
        self._running = True
        backoff = 1.0
        max_backoff = 60.0

        while self._running:
            try:
                logger.info("Connecting to Herald Relay at %s …", self.relay_url)
                async with websockets.connect(
                    self.relay_url,
                    extra_headers={"Authorization": f"Bearer {self.device_token}"},
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    backoff = 1.0  # reset on successful connect
                    logger.info("Connected to Herald Relay.")
                    await self._send(ws, {"type": "register", "device_token": self.device_token})
                    await self._message_loop(ws)
            except asyncio.CancelledError:
                logger.info("HeraldRelayClient cancelled — stopping.")
                break
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                if not self._running:
                    break
                logger.warning("Herald Relay disconnected: %s. Reconnecting in %.0fs …", exc, backoff)
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                logger.exception("Unexpected error in relay loop: %s. Reconnecting in %.0fs …", exc, backoff)
            finally:
                self._ws = None

            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)

        logger.info("HeraldRelayClient stopped.")

    async def send_push_trigger(
        self,
        message: str,
        urgency: str = "low",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Send a push notification trigger to Herald Cloud."""
        if self._ws is None:
            logger.warning("Cannot send push trigger — not connected to Herald Relay.")
            return
        payload: dict[str, Any] = {
            "type": "push_trigger",
            "message": message,
            "urgency": urgency,
        }
        if metadata:
            payload["metadata"] = metadata
        try:
            await self._send(self._ws, payload)
            logger.debug("Push trigger sent: [%s] %s", urgency, message)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to send push trigger: %s", exc)

    async def close(self) -> None:
        """Signal the run loop to stop and close the WebSocket."""
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        logger.info("HeraldRelayClient close() called.")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _message_loop(self, ws: Any) -> None:
        """Read messages from Herald Cloud and dispatch them."""
        async for raw in ws:
            if not self._running:
                break
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Received non-JSON message from relay: %r", raw)
                continue

            msg_type = msg.get("type")
            logger.debug("← relay message type=%s", msg_type)

            if msg_type == "forward_request":
                asyncio.create_task(self._handle_forward_request(ws, msg))
            elif msg_type == "ping":
                await self._send(ws, {"type": "pong"})
            else:
                logger.debug("Unhandled relay message type: %s", msg_type)

    async def _handle_forward_request(
        self, ws: Any, msg: dict
    ) -> None:
        """Proxy a forward_request from Herald Cloud to local Hermes."""
        request_id: str = msg.get("request_id", "unknown")
        method: str = msg.get("method", "GET").upper()
        path: str = msg.get("path", "/")
        body: dict | None = msg.get("body")
        headers: dict = msg.get("headers", {})

        url = f"{self.local_hermes_url}{path}"
        logger.info("→ Hermes %s %s (request_id=%s)", method, path, request_id)

        if _is_sse_path(method, path):
            await self._handle_sse_request(ws, request_id, method, url, body, headers)
        else:
            await self._handle_regular_request(ws, request_id, method, url, body, headers)

    async def _handle_regular_request(
        self,
        ws: Any,
        request_id: str,
        method: str,
        url: str,
        body: dict | None,
        headers: dict,
    ) -> None:
        """Call local Hermes and send a single forward_response."""
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.request(
                    method,
                    url,
                    json=body,
                    headers={k: v for k, v in headers.items() if k.lower() not in ("host",)},
                )
            resp_body: Any
            try:
                resp_body = response.json()
            except Exception:
                resp_body = response.text

            await self._send(
                ws,
                {
                    "type": "forward_response",
                    "request_id": request_id,
                    "status": response.status_code,
                    "body": resp_body,
                },
            )
            logger.info("← Hermes %d (request_id=%s)", response.status_code, request_id)

        except httpx.ConnectError as exc:
            logger.error("Local Hermes unreachable for request %s: %s", request_id, exc)
            await self._send_error_response(ws, request_id, 503, "Local Hermes is unreachable")
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error forwarding request %s: %s", request_id, exc)
            await self._send_error_response(ws, request_id, 500, str(exc))

    async def _handle_sse_request(
        self,
        ws: Any,
        request_id: str,
        method: str,
        url: str,
        body: dict | None,
        headers: dict,
    ) -> None:
        """Stream an SSE response from local Hermes back to Herald Cloud as sse_chunk messages."""
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    method,
                    url,
                    json=body,
                    headers={k: v for k, v in headers.items() if k.lower() not in ("host",)},
                ) as response:
                    # Notify cloud of status first
                    await self._send(
                        ws,
                        {
                            "type": "sse_start",
                            "request_id": request_id,
                            "status": response.status_code,
                        },
                    )

                    if response.status_code >= 400:
                        body_text = await response.aread()
                        await self._send(
                            ws,
                            {
                                "type": "sse_end",
                                "request_id": request_id,
                                "error": body_text.decode(errors="replace"),
                            },
                        )
                        return

                    async for line in response.aiter_lines():
                        if not self._running:
                            break
                        if line.startswith("data:"):
                            data_payload = line[len("data:"):].strip()
                            await self._send(
                                ws,
                                {
                                    "type": "sse_chunk",
                                    "request_id": request_id,
                                    "data": data_payload,
                                },
                            )
                        elif line.startswith("event:"):
                            # forward event name as metadata
                            await self._send(
                                ws,
                                {
                                    "type": "sse_event",
                                    "request_id": request_id,
                                    "event": line[len("event:"):].strip(),
                                },
                            )
                        # blank lines and id: lines are intentionally skipped

            await self._send(ws, {"type": "sse_end", "request_id": request_id})
            logger.info("SSE stream complete (request_id=%s)", request_id)

        except httpx.ConnectError as exc:
            logger.error("Local Hermes unreachable for SSE request %s: %s", request_id, exc)
            await self._send(
                ws,
                {"type": "sse_end", "request_id": request_id, "error": "Local Hermes is unreachable"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error in SSE stream %s: %s", request_id, exc)
            await self._send(
                ws,
                {"type": "sse_end", "request_id": request_id, "error": str(exc)},
            )

    async def _send_error_response(
        self,
        ws: Any,
        request_id: str,
        status: int,
        message: str,
    ) -> None:
        await self._send(
            ws,
            {
                "type": "forward_response",
                "request_id": request_id,
                "status": status,
                "body": {"error": message},
            },
        )

    async def _send(self, ws: Any, payload: dict) -> None:
        """Serialise and send a JSON message, serialising concurrent sends."""
        async with self._send_lock:
            await ws.send(json.dumps(payload))
