"""
Herald Relay – WebSocket endpoint for Hermes plugin connections.

Protocol (plugin → server)
--------------------------
  {"type": "register",          "device_token": "...", "hermes_version": "..."}
  {"type": "forward_response",  "request_id": "uuid",  "status": 200, "body": {...}, "headers": {}}
  {"type": "sse_chunk",         "request_id": "uuid",  "data": "event:…\\ndata:…\\n\\n"}
  {"type": "sse_done",          "request_id": "uuid"}
  {"type": "push_trigger",      "message": "...",       "urgency": "low|high", "metadata": {}}
  {"type": "pong"}

Protocol (server → plugin)
--------------------------
  {"type": "registered",        "relay_id": "uuid"}
  {"type": "forward_request",   "request_id": "uuid",  "method": "GET|POST",
                                 "path": "/v1/runs",    "body": null|{}, "headers": {}}
  {"type": "ping"}
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from services.relay_manager import relay_manager

logger = logging.getLogger(__name__)

router = APIRouter()

PING_INTERVAL = 30  # seconds


@router.websocket("/connect")
async def relay_connect(websocket: WebSocket, device_token: str = ""):
    """Main WebSocket endpoint for Hermes plugin connections."""
    await websocket.accept()
    logger.info("WS accepted, waiting for register (device_token hint=%r)", device_token)

    # -----------------------------------------------------------------------
    # Step 1 – wait for 'register' message
    # -----------------------------------------------------------------------
    registered_token: str | None = None
    relay_id: str | None = None

    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=15.0)
    except asyncio.TimeoutError:
        logger.warning("Registration timeout – closing WS")
        await websocket.close(code=4008, reason="registration timeout")
        return
    except WebSocketDisconnect:
        logger.info("Client disconnected before registering")
        return

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await websocket.close(code=4003, reason="invalid JSON")
        return

    if msg.get("type") != "register":
        await websocket.close(code=4003, reason="expected register message")
        return

    registered_token = msg.get("device_token") or device_token
    hermes_version = msg.get("hermes_version")

    if not registered_token:
        await websocket.close(code=4003, reason="device_token required")
        return

    relay_id = await relay_manager.register(registered_token, websocket)
    await websocket.send_text(json.dumps({"type": "registered", "relay_id": relay_id}))
    logger.info(
        "Plugin registered  device=%s  hermes_version=%s  relay_id=%s",
        registered_token,
        hermes_version,
        relay_id,
    )

    # -----------------------------------------------------------------------
    # Step 2 – concurrent ping loop + receive loop
    # -----------------------------------------------------------------------
    ping_task = asyncio.create_task(_ping_loop(websocket, registered_token))

    try:
        await _receive_loop(websocket, registered_token)
    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: device=%s", registered_token)
    except Exception as exc:
        logger.exception("Unexpected error in relay receive loop for device=%s: %s", registered_token, exc)
    finally:
        ping_task.cancel()
        await relay_manager.unregister(registered_token)
        logger.info("Cleaned up relay for device=%s", registered_token)


async def _ping_loop(websocket: WebSocket, device_token: str) -> None:
    """Send a ping every PING_INTERVAL seconds to keep the connection alive."""
    try:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await websocket.send_text(json.dumps({"type": "ping"}))
                logger.debug("Sent ping to device=%s", device_token)
            except Exception:
                # Socket probably closed; receive loop will handle cleanup.
                break
    except asyncio.CancelledError:
        pass


async def _receive_loop(websocket: WebSocket, device_token: str) -> None:
    """Process incoming messages from the Hermes plugin."""
    while True:
        raw = await websocket.receive_text()
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Non-JSON message from device=%s: %r", device_token, raw[:200])
            continue

        msg_type = msg.get("type")
        logger.debug("← device=%s  type=%s", device_token, msg_type)

        if msg_type == "forward_response":
            request_id = msg.get("request_id", "")
            await relay_manager.handle_forward_response(request_id, msg)

        elif msg_type == "sse_chunk":
            request_id = msg.get("request_id", "")
            data = msg.get("data", "")
            await relay_manager.handle_sse_chunk(request_id, data)

        elif msg_type == "sse_done":
            request_id = msg.get("request_id", "")
            await relay_manager.handle_sse_done(request_id)

        elif msg_type == "push_trigger":
            message = msg.get("message", "")
            urgency = msg.get("urgency", "low")
            metadata = msg.get("metadata", {})
            asyncio.create_task(
                relay_manager.trigger_push(device_token, message, urgency, metadata)
            )

        elif msg_type == "pong":
            logger.debug("Pong from device=%s", device_token)

        else:
            logger.warning("Unknown message type %r from device=%s", msg_type, device_token)
