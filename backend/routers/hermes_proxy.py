"""Proxy HTTP requests from the Flutter app through the relay tunnel to local Hermes.

Transport note
--------------
The hermes-herald plugin connects over **SSE** (``GET /tunnel/events``), not a
WebSocket. ``relay_manager`` only tracks WebSocket registrations, so its
registry is permanently empty for SSE plugins -- every ``/hermes/*`` call used
to answer ``device_offline`` even with a healthy tunnel, and ``/hermes/health``
reported ``hermes_connected: false``.

These endpoints therefore prefer the SSE tunnel (``routers.tunnel``) and fall
back to the WebSocket relay when one is registered, so both transports work.
"""
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from services.relay_manager import relay_manager
from routers import tunnel as tunnel_mod
import asyncio
import json
import logging
from typing import NoReturn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hermes", tags=["hermes-proxy"])

SSE_PATHS = {"/v1/runs", "/v1/chat/completions"}


def _device_token(request: Request) -> str:
    token = (
        request.headers.get("X-Device-Token")
        or request.headers.get("device_token")
        or request.query_params.get("device_token")
    )
    if not token:
        # Single-tenant fallback: when exactly ONE plugin is connected, use it.
        #
        # Upstream callers (the Herald backend's HermesClient) build request
        # URLs as f"{base_url}{path}", so a base URL carrying a query string
        # produces ".../hermes?device_token=X/v1/models" -> 404. There is no
        # header passthrough for a device token either, which left NO way to
        # address the tunnel from the Herald backend at all. Resolving the sole
        # connected device keeps self-hosted single-user setups working with a
        # clean base URL. Multi-tenant callers must still send the token.
        connected = list(tunnel_mod._plugin_queues.keys())
        if len(connected) == 1:
            return connected[0]
        raise HTTPException(400, "Missing device_token header or query param")
    return token


def _is_connected(token: str) -> bool:
    """Connected over EITHER transport."""
    return tunnel_mod.is_plugin_connected(token) or relay_manager.is_connected(token)


def _offline_response() -> NoReturn:
    raise HTTPException(
        503,
        detail={
            "error": "device_offline",
            "message": "Hermes is not connected. Make sure the herald-relay plugin is running on your local Hermes.",
        },
    )


async def _proxy(token: str, method: str, path: str, body=None, headers: dict | None = None):
    """Single request/response round-trip over whichever transport is live."""
    if tunnel_mod.is_plugin_connected(token):
        try:
            result = await tunnel_mod.forward_http(token, method, path, body, headers or {})
        except ConnectionError:
            _offline_response()
        except asyncio.TimeoutError:
            raise HTTPException(
                504,
                detail={
                    "error": "hermes_timeout",
                    "message": "Local Hermes did not respond in time.",
                },
            )
        return Response(
            content=json.dumps(result.get("body")),
            media_type="application/json",
            status_code=result.get("status", 200),
        )

    if relay_manager.is_connected(token):
        async for item in relay_manager.forward_request(token, method, path, body, headers or {}):
            if item.get("type") == "response":
                return Response(
                    content=json.dumps(item["body"]),
                    media_type="application/json",
                    status_code=item["status"],
                )
    _offline_response()


@router.get("/health")
async def hermes_health(request: Request):
    token = _device_token(request)
    connected = _is_connected(token)
    # Return relay connection status immediately — don't block on a tunnel round-trip.
    # Flutter uses this to show the "Hermes connected" indicator.
    return {"hermes_connected": connected, "relay_connected": connected}


@router.get("/health/detailed")
async def hermes_health_detailed(request: Request):
    """Debug-panel health. Herald's HermesClient.health() calls this path."""
    token = _device_token(request)
    connected = _is_connected(token)
    if not connected:
        return {
            "status": "offline",
            "hermes_connected": False,
            "relay_connected": False,
        }
    try:
        result = await tunnel_mod.forward_http(token, "GET", "/health", None, {})
        return {
            "status": "ok",
            "hermes_connected": True,
            "relay_connected": True,
            "hermes": result.get("body"),
        }
    except (ConnectionError, asyncio.TimeoutError):
        # Tunnel is up but local Hermes did not answer.
        return {"status": "degraded", "hermes_connected": True, "relay_connected": True}


@router.get("/v1/models")
async def get_models(request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    return await _proxy(token, "GET", "/v1/models")


@router.get("/v1/channels")
async def get_channels(request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    return await _proxy(token, "GET", "/v1/channels")


async def _sse_generator(token: str, method: str, path: str, body, headers: dict):
    """SSE passthrough.

    Streams the plugin's response chunks through as they arrive so spoken
    checkpoints reach the user DURING a long run. Older plugins that answer
    with a single ``/tunnel/http_response`` still work — ``open_stream()``
    falls back to emitting that one payload.
    """
    if tunnel_mod.is_plugin_connected(token):
        try:
            async for chunk in tunnel_mod.open_stream(
                token, method, path, body, headers, timeout=300.0
            ):
                if not chunk:
                    continue
                text = chunk if isinstance(chunk, str) else json.dumps(chunk)
                # Pass through already-framed SSE verbatim; otherwise frame it.
                if text.lstrip().startswith("data:"):
                    yield text if text.endswith("\n\n") else text + "\n\n"
                else:
                    yield f"data: {text}\n\n"
        except ConnectionError:
            yield f"data: {json.dumps({'error': 'device_offline'})}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'error': 'hermes_timeout'})}\n\n"
        yield "data: [DONE]\n\n"
        return

    async for item in relay_manager.forward_request(token, method, path, body, headers):
        if item["type"] == "sse_chunk":
            yield item["data"]
        elif item["type"] == "response":
            yield f"data: {json.dumps(item['body'])}\n\n"
        elif item["type"] == "sse_done":
            yield "data: [DONE]\n\n"
            break
        elif item["type"] == "error":
            yield f"data: {json.dumps({'error': item['message']})}\n\n"
            break


@router.post("/v1/runs")
async def start_run(request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    body = await request.json()
    return StreamingResponse(
        _sse_generator(token, "POST", "/v1/runs", body, {}), media_type="text/event-stream"
    )


@router.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    return StreamingResponse(
        _sse_generator(token, "GET", f"/v1/runs/{run_id}/events", None, {}),
        media_type="text/event-stream",
    )


@router.post("/v1/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    return await _proxy(token, "POST", f"/v1/runs/{run_id}/stop")


@router.post("/v1/runs/{run_id}/approval")
async def approve_run(run_id: str, request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()
    body = await request.json()
    return await _proxy(token, "POST", f"/v1/runs/{run_id}/approval", body)


# ---------------------------------------------------------------------------
# Generic passthrough — MUST be declared last so the explicit routes above win.
# ---------------------------------------------------------------------------
#
# Herald calls more of the Hermes API than the hand-written routes covered
# (e.g. POST /api/sessions for durable conversation sessions), and every
# uncovered path returned a confusing 404 that surfaced as
# "create_session failed: HTTP 404" with session continuity silently broken.
# Forwarding anything under /hermes/** keeps the proxy in step with the Hermes
# API without needing a new route per endpoint.
@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
)
async def passthrough(full_path: str, request: Request):
    token = _device_token(request)
    if not _is_connected(token):
        _offline_response()

    body = None
    if request.method in ("POST", "PUT", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None

    path = "/" + full_path.lstrip("/")
    query = request.url.query
    # Drop device_token from the forwarded query — it addresses the tunnel,
    # not the Hermes endpoint behind it.
    if query:
        kept = "&".join(
            p for p in query.split("&") if not p.startswith("device_token=")
        )
        if kept:
            path = f"{path}?{kept}"

    return await _proxy(token, request.method, path, body)
