"""Proxy HTTP requests from the Flutter app through the WS relay tunnel to local Hermes."""
from fastapi import APIRouter, Request, Response, HTTPException
from fastapi.responses import StreamingResponse
from services.relay_manager import relay_manager
import json, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/hermes", tags=["hermes-proxy"])

SSE_PATHS = {"/v1/runs", "/v1/chat/completions"}

def _device_token(request: Request) -> str:
    token = request.headers.get("X-Device-Token") or request.query_params.get("device_token")
    if not token:
        raise HTTPException(400, "Missing device_token header or query param")
    return token

def _offline_response():
    raise HTTPException(503, detail={"error": "device_offline", "message": "Hermes is not connected. Make sure the herald-relay plugin is running on your local Hermes."})

@router.get("/health")
async def hermes_health(request: Request):
    token = _device_token(request)
    connected = relay_manager.is_connected(token)
    # Return relay connection status immediately — don't block on a tunnel round-trip.
    # Flutter uses this to show the "Hermes connected" indicator.
    return {"hermes_connected": connected, "relay_connected": connected}

@router.get("/v1/models")
async def get_models(request: Request):
    token = _device_token(request)
    if not relay_manager.is_connected(token): _offline_response()
    async for item in relay_manager.forward_request(token, "GET", "/v1/models", None, {}):
        if item.get("type") == "response":
            return Response(content=json.dumps(item["body"]), media_type="application/json", status_code=item["status"])
    _offline_response()

async def _sse_generator(token: str, method: str, path: str, body, headers: dict):
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
    if not relay_manager.is_connected(token): _offline_response()
    body = await request.json()
    return StreamingResponse(_sse_generator(token, "POST", "/v1/runs", body, {}), media_type="text/event-stream")

@router.get("/v1/runs/{run_id}/events")
async def run_events(run_id: str, request: Request):
    token = _device_token(request)
    if not relay_manager.is_connected(token): _offline_response()
    return StreamingResponse(_sse_generator(token, "GET", f"/v1/runs/{run_id}/events", None, {}), media_type="text/event-stream")

@router.post("/v1/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request):
    token = _device_token(request)
    if not relay_manager.is_connected(token): _offline_response()
    async for item in relay_manager.forward_request(token, "POST", f"/v1/runs/{run_id}/stop", None, {}):
        if item.get("type") == "response":
            return Response(content=json.dumps(item["body"]), media_type="application/json", status_code=item["status"])

@router.post("/v1/runs/{run_id}/approval")
async def approve_run(run_id: str, request: Request):
    token = _device_token(request)
    if not relay_manager.is_connected(token): _offline_response()
    body = await request.json()
    async for item in relay_manager.forward_request(token, "POST", f"/v1/runs/{run_id}/approval", body, {}):
        if item.get("type") == "response":
            return Response(content=json.dumps(item["body"]), media_type="application/json", status_code=item["status"])
