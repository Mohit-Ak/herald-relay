"""
SSE+POST tunnel for cloud ↔ hermes-herald plugin communication.

Endpoints (mounted at /tunnel in main.py):
  POST /tunnel/connect    – plugin registers, sends AgentCard
  GET  /tunnel/events     – plugin subscribes to SSE stream (Cloud→Plugin)
  POST /tunnel/update     – plugin sends classified events back (Plugin→Cloud)

Monitoring endpoint (mounted at / in main.py):
  GET  /monitor/{device_token}/{run_id} – Flutter subscribes to SSE (Cloud→Flutter)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.firestore_client import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Two routers: router (prefix /tunnel) and monitor_router (prefix "")
# ---------------------------------------------------------------------------
router = APIRouter(tags=["tunnel"])           # mounted at /tunnel in main.py
monitor_router = APIRouter(tags=["monitor"])  # mounted at / in main.py

# ---------------------------------------------------------------------------
# In-memory SSE queues (transient – intentionally lost on restart)
# ---------------------------------------------------------------------------
_plugin_queues: dict[str, asyncio.Queue] = {}               # device_token → Queue
_flutter_queues: dict[str, dict[str, asyncio.Queue]] = {}   # device_token → {run_id → Queue}

# request_id → Future resolved by POST /tunnel/http_response.
# Backs the HTTP-over-SSE forwarding used by the /hermes/* proxy: the plugin
# connects with SSE (not a WebSocket), so relay_manager's WS registry is always
# empty for it and cannot be used to reach a plugin-side Hermes.
_http_waiters: dict[str, asyncio.Future] = {}
# Per-request chunk queues for STREAMING forwards (SSE run events). Separate
# from _http_waiters, which holds single-shot replies.
_stream_waiters: dict[str, asyncio.Queue] = {}

# How long the proxy waits for the plugin to answer a forwarded request.
HTTP_FORWARD_TIMEOUT_S = 30.0


def is_plugin_connected(device_token: str) -> bool:
    """True when the plugin holds a live GET /tunnel/events SSE stream."""
    return device_token in _plugin_queues


def plugin_connected_count() -> int:
    return len(_plugin_queues)


async def forward_http(
    device_token: str,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = HTTP_FORWARD_TIMEOUT_S,
) -> dict:
    """Forward an HTTP request to the plugin over SSE and await its reply.

    Returns ``{"status": int, "body": Any, "headers": dict}``.
    Raises ConnectionError when the plugin is not subscribed, TimeoutError
    when it does not answer in time.
    """
    queue = _plugin_queues.get(device_token)
    if queue is None:
        raise ConnectionError("plugin not connected")

    request_id = str(uuid.uuid4())
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _http_waiters[request_id] = fut
    try:
        await queue.put(
            {
                "type": "forward_request",
                "request_id": request_id,
                "method": method,
                "path": path,
                "body": body,
                "headers": headers or {},
            }
        )
        return await asyncio.wait_for(fut, timeout=timeout)
    finally:
        _http_waiters.pop(request_id, None)


class HttpResponseRequest(BaseModel):
    device_token: str
    request_id: str
    status: int = 200
    body: object = None
    headers: dict = {}
    error: Optional[str] = None


class HttpStreamChunkRequest(BaseModel):
    """One incremental chunk of a streaming (SSE) forwarded response.

    The plugin POSTs these as upstream data arrives, instead of buffering the
    whole run and answering once. Without it, a 75-second Hermes run delivered
    every spoken checkpoint in a single burst at the END — the user heard
    nothing for over a minute, which is exactly the silence checkpoints exist
    to prevent.
    """

    device_token: str
    request_id: str
    chunk: str
    done: bool = False


async def open_stream(
    device_token: str,
    method: str,
    path: str,
    body: dict | None = None,
    headers: dict | None = None,
    timeout: float = HTTP_FORWARD_TIMEOUT_S,
):
    """Forward a request and yield the plugin's response chunks as they land.

    Falls back to a single-shot reply if the plugin is an older build that
    answers with ``/tunnel/http_response`` instead of streaming chunks.
    """
    queue = _plugin_queues.get(device_token)
    if queue is None:
        raise ConnectionError("plugin not connected")

    request_id = str(uuid.uuid4())
    chunk_q: asyncio.Queue = asyncio.Queue()
    _stream_waiters[request_id] = chunk_q
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    _http_waiters[request_id] = fut

    try:
        await queue.put(
            {
                "type": "forward_request",
                "request_id": request_id,
                "method": method,
                "path": path,
                "body": body,
                "headers": headers or {},
                "stream": True,
            }
        )

        while True:
            chunk_task = asyncio.ensure_future(chunk_q.get())
            done_set, _ = await asyncio.wait(
                {chunk_task, fut},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done_set:
                chunk_task.cancel()
                raise asyncio.TimeoutError()

            if chunk_task in done_set:
                item = chunk_task.result()
                if item is None:  # end-of-stream sentinel
                    return
                yield item
                continue

            chunk_task.cancel()
            # Legacy single-shot reply from an older plugin.
            result = fut.result()
            payload = result.get("body")
            if payload is not None:
                yield payload if isinstance(payload, str) else json.dumps(payload)
            return
    finally:
        _http_waiters.pop(request_id, None)
        _stream_waiters.pop(request_id, None)



# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

class TunnelConnectRequest(BaseModel):
    device_token: str
    agent_card: dict          # AgentCard JSON
    hermes_version: str = "unknown"


class TunnelUpdateRequest(BaseModel):
    device_token: str
    run_id: str
    seq: int                  # sequence number for ordering
    signal: str               # IGNORE|ACCUMULATE|MILESTONE|QUESTION|DONE
    event: dict               # original Hermes event
    summary: Optional[str] = None       # for MILESTONE/DONE
    spoken_text: Optional[str] = None   # for QUESTION/DONE — what to say aloud


class A2ATask(BaseModel):
    task_id: str              # uuid
    device_token: str
    run_id: Optional[str] = None
    command: str              # slash command or plain text
    created_at: str
    status: str = "pending"   # pending|working|input_required|completed|failed


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _require_device(device_token: str) -> dict:
    """Raise 404 if device_token is not registered in Firestore."""
    db = get_db()
    doc = db.collection("devices").document(device_token).get()
    if not doc.exists:
        raise HTTPException(status_code=404, detail="Device not registered")
    return doc.to_dict()


async def _send_fcm(
    device_token: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> None:
    """
    Send a high-priority FCM push notification to *device_token*.
    Reuses the FCM credential flow from routers/push.py.
    """
    import os
    import httpx

    FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "")
    db = get_db()
    doc = db.collection("devices").document(device_token).get()
    if not doc.exists:
        logger.warning(f"[FCM] device {device_token[:8]}... not found – skipping push")
        return
    fcm_token = doc.to_dict().get("fcm_token")
    if not fcm_token:
        logger.warning(f"[FCM] no fcm_token for {device_token[:8]}... – skipping push")
        return

    if not FCM_PROJECT_ID:
        logger.info(f"[FCM STUB] push → {device_token[:8]}... title={title!r} body={body!r}")
        return

    try:
        import google.auth
        import google.auth.transport.requests

        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/firebase.messaging"]
        )
        credentials.refresh(google.auth.transport.requests.Request())
        access_token = credentials.token
    except Exception as exc:
        logger.error(f"[FCM] credential error: {exc}")
        return

    payload: dict = {
        "message": {
            "token": fcm_token,
            "notification": {"title": title, "body": body or ""},
            "data": {k: str(v) for k, v in (data or {}).items()},
            "android": {"priority": "high"},
            "apns": {"headers": {"apns-priority": "10"}},
        }
    }
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
                timeout=10.0,
            )
            resp.raise_for_status()
        logger.info(f"[FCM] push sent to {device_token[:8]}...")
    except Exception as exc:
        logger.error(f"[FCM] send failed for {device_token[:8]}...: {exc}")


async def _event_generator(queue: asyncio.Queue, heartbeat_interval: int = 25):
    """
    Async generator that yields SSE-formatted strings from *queue*.
    Sends a keep-alive heartbeat comment every *heartbeat_interval* seconds.
    Exits cleanly on cancellation.
    """
    while True:
        try:
            data = await asyncio.wait_for(queue.get(), timeout=heartbeat_interval)
            yield f"data: {json.dumps(data)}\n\n"
        except asyncio.TimeoutError:
            yield ": heartbeat\n\n"
        except asyncio.CancelledError:
            return


# ---------------------------------------------------------------------------
# Public helper – dispatch an A2A task to a connected plugin
# ---------------------------------------------------------------------------

async def dispatch_task(device_token: str, task: A2ATask) -> None:
    """
    Deliver *task* to the plugin identified by *device_token*.

    If the plugin has an active SSE connection the task is pushed immediately
    via the in-memory queue.  If not, it is persisted in Firestore with
    status='queued' so the plugin can replay it on the next /tunnel/events
    connection via Last-Event-ID.
    """
    if device_token in _plugin_queues:
        await _plugin_queues[device_token].put(
            {"type": "task", "task": task.model_dump()}
        )
        logger.info(f"[dispatch_task] task {task.task_id} enqueued for {device_token[:8]}...")
    else:
        db = get_db()
        db.collection("tasks").document(task.task_id).set(
            {**task.model_dump(), "status": "queued", "queued_at": time.time()}
        )
        logger.info(
            f"[dispatch_task] plugin {device_token[:8]}... offline – "
            f"task {task.task_id} stored as queued"
        )


# ---------------------------------------------------------------------------
# POST /tunnel/connect
# ---------------------------------------------------------------------------

@router.post("/connect", summary="Plugin registers and sends AgentCard")
async def tunnel_connect(req: TunnelConnectRequest):
    """
    Called by the hermes-herald plugin on startup.
    Verifies the device exists, caches the AgentCard, and returns the SSE URL.
    """
    _require_device(req.device_token)

    db = get_db()
    db.collection("devices").document(req.device_token).update(
        {
            "agent_card": req.agent_card,
            "hermes_version": req.hermes_version,
            "last_seen": time.time(),
        }
    )
    logger.info(
        f"[tunnel/connect] device={req.device_token[:8]}... "
        f"hermes_version={req.hermes_version}"
    )
    return {"ok": True, "tunnel_url": "/tunnel/events"}


# ---------------------------------------------------------------------------
# GET /tunnel/events  – SSE stream Cloud → Plugin
# ---------------------------------------------------------------------------

@router.get("/events", summary="Plugin subscribes to SSE task stream")
async def tunnel_events(
    device_token: Optional[str] = Query(None, description="Device token (query param auth)"),
    authorization: Optional[str] = Header(None),
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
):
    """
    Long-lived SSE stream consumed by the hermes-herald plugin.

    Authentication: pass *device_token* as a query param **or** as a Bearer token
    in the ``Authorization`` header.

    On reconnect, supply ``Last-Event-ID`` to trigger replay of any tasks that
    were queued in Firestore while the plugin was offline.
    """
    # Resolve token from query param or Bearer header
    token: Optional[str] = device_token
    if not token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]
    if not token:
        raise HTTPException(
            status_code=400,
            detail="device_token required via query param or Authorization: Bearer <token>",
        )

    _require_device(token)

    queue: asyncio.Queue = asyncio.Queue()
    _plugin_queues[token] = queue
    logger.info(f"[tunnel/events] plugin connected: {token[:8]}...")

    # Replay tasks queued while plugin was offline
    if last_event_id is not None:
        try:
            db = get_db()
            queued = (
                db.collection("tasks")
                .where("device_token", "==", token)
                .where("status", "==", "queued")
                .order_by("queued_at")
                .stream()
            )
            for doc in queued:
                t = doc.to_dict()
                await queue.put({"type": "task", "task": t})
                doc.reference.update({"status": "replayed"})
                logger.info(f"[tunnel/events] replayed task {doc.id} to {token[:8]}...")
        except Exception as exc:
            logger.warning(f"[tunnel/events] Last-Event-ID replay failed: {exc}")

    async def _generator():
        try:
            async for chunk in _event_generator(queue):
                yield chunk
        finally:
            if _plugin_queues.get(token) is queue:
                del _plugin_queues[token]
            logger.info(f"[tunnel/events] plugin disconnected: {token[:8]}...")

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# POST /tunnel/update  – Plugin → Cloud classified events / results
# ---------------------------------------------------------------------------

@router.post("/http_response", summary="Plugin returns a forwarded HTTP response")
async def tunnel_http_response(req: HttpResponseRequest):
    """Completion half of the HTTP-over-SSE forwarding started by forward_http().

    The plugin receives ``{"type": "forward_request", ...}`` on its SSE stream,
    performs the call against its local Hermes, and POSTs the result here.
    """
    fut = _http_waiters.get(req.request_id)
    if fut is None or fut.done():
        # Late or duplicate reply — the proxy already gave up.
        return {"ok": False, "reason": "no_waiter"}

    if req.error:
        fut.set_result({"status": 502, "body": {"error": req.error}, "headers": {}})
    else:
        fut.set_result(
            {"status": req.status, "body": req.body, "headers": req.headers or {}}
        )
    return {"ok": True}


@router.post("/http_stream", summary="Plugin streams an incremental response chunk")
async def tunnel_http_stream(req: HttpStreamChunkRequest):
    """Incremental half of a STREAMING forward started by ``open_stream()``.

    The plugin POSTs each SSE chunk here the moment its local Hermes produces
    it, so spoken checkpoints reach the user DURING a long run rather than all
    at once when it ends. ``done=True`` closes the stream.
    """
    q = _stream_waiters.get(req.request_id)
    if q is None:
        return {"ok": False, "reason": "no_waiter"}
    if req.chunk:
        await q.put(req.chunk)
    if req.done:
        await q.put(None)  # end-of-stream sentinel
    return {"ok": True}


@router.post("/update", summary="Plugin sends classified events / task results")
async def tunnel_update(req: TunnelUpdateRequest):
    """
    Receives a classified Hermes event from the plugin.

    - Always persists to Firestore ``tasks/{task_id}/updates/{seq}``.
    - ``MILESTONE``: updates task status to *working* in Firestore.
    - ``QUESTION``: updates task status to *input_required* + sends FCM push.
    - ``DONE``: updates task status to *completed* + sends FCM push.
    - All signals except ``IGNORE``: forwarded to active Flutter SSE monitor.
    """
    _require_device(req.device_token)

    db = get_db()

    # task_id is expected inside the event payload; fall back to run_id
    task_id: str = req.event.get("task_id") or req.run_id

    # --- Persist update ---
    try:
        db.collection("tasks").document(task_id).collection("updates").document(
            str(req.seq)
        ).set(
            {
                "seq": req.seq,
                "signal": req.signal,
                "event": req.event,
                "summary": req.summary,
                "spoken_text": req.spoken_text,
                "received_at": time.time(),
            }
        )
    except Exception as exc:
        logger.error(f"[tunnel/update] Firestore write failed: {exc}")
        raise HTTPException(status_code=500, detail="Firestore write failed")

    # --- Update task document status ---
    try:
        if req.signal == "MILESTONE":
            db.collection("tasks").document(task_id).set(
                {"status": "working", "last_milestone_seq": req.seq, "run_id": req.run_id},
                merge=True,
            )
        elif req.signal == "QUESTION":
            db.collection("tasks").document(task_id).set(
                {"status": "input_required", "run_id": req.run_id},
                merge=True,
            )
        elif req.signal == "DONE":
            db.collection("tasks").document(task_id).set(
                {"status": "completed", "completed_at": time.time(), "run_id": req.run_id},
                merge=True,
            )
    except Exception as exc:
        logger.warning(f"[tunnel/update] task status update failed (signal={req.signal}): {exc}")

    # --- FCM push for QUESTION / DONE ---
    if req.signal == "QUESTION":
        await _send_fcm(
            req.device_token,
            title="Hermes needs your input",
            body=req.spoken_text or "",
            data={"task_id": task_id, "run_id": req.run_id, "signal": req.signal},
        )
    elif req.signal == "DONE":
        await _send_fcm(
            req.device_token,
            title="Task complete",
            body=req.summary or "",
            data={"task_id": task_id, "run_id": req.run_id, "signal": req.signal},
        )

    # --- Forward to Flutter SSE monitor (skip IGNORE) ---
    if req.signal != "IGNORE":
        flutter_q = _flutter_queues.get(req.device_token, {}).get(req.run_id)
        if flutter_q is not None:
            payload = {
                "type": "update",
                "seq": req.seq,
                "signal": req.signal,
                "event": req.event,
                "summary": req.summary,
                "spoken_text": req.spoken_text,
            }
            try:
                flutter_q.put_nowait(payload)
            except asyncio.QueueFull:
                logger.warning(
                    f"[tunnel/update] Flutter queue full for "
                    f"{req.device_token[:8]}... run={req.run_id}"
                )

    logger.info(
        f"[tunnel/update] device={req.device_token[:8]}... "
        f"run={req.run_id} seq={req.seq} signal={req.signal}"
    )
    return {"ok": True}


# ---------------------------------------------------------------------------
# GET /monitor/{device_token}/{run_id}  – SSE stream Cloud → Flutter
# Mounted on monitor_router (prefix="") in main.py
# ---------------------------------------------------------------------------

@monitor_router.get(
    "/monitor/{device_token}/{run_id}",
    summary="Flutter monitors a run via SSE",
)
async def monitor_run(device_token: str, run_id: str):
    """
    Flutter opens this endpoint to receive live filtered events for a specific run.
    Events are forwarded here from POST /tunnel/update (all signals except IGNORE).
    """
    _require_device(device_token)

    queue: asyncio.Queue = asyncio.Queue(maxsize=256)

    if device_token not in _flutter_queues:
        _flutter_queues[device_token] = {}
    _flutter_queues[device_token][run_id] = queue
    logger.info(f"[monitor] Flutter connected: device={device_token[:8]}... run={run_id}")

    async def _generator():
        try:
            async for chunk in _event_generator(queue):
                yield chunk
        finally:
            run_map = _flutter_queues.get(device_token, {})
            if run_map.get(run_id) is queue:
                del run_map[run_id]
            if not run_map and device_token in _flutter_queues:
                del _flutter_queues[device_token]
            logger.info(
                f"[monitor] Flutter disconnected: device={device_token[:8]}... run={run_id}"
            )

    return StreamingResponse(
        _generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# POST /tunnel/dispatch  – Cloud/test injects an A2A task to a device
# ---------------------------------------------------------------------------

class DispatchRequest(BaseModel):
    device_token: str
    task: A2ATask


# ---------------------------------------------------------------------------
# POST /tunnel/approval  – Flutter sends approve/deny for a QUESTION event
# ---------------------------------------------------------------------------

class ApprovalRequest(BaseModel):
    device_token: str
    run_id: str
    approved: bool
    message: Optional[str] = None   # optional free-text response


@router.get("/pending_approvals", summary="Plugin fetches offline-queued approvals on reconnect")
async def get_pending_approvals(device_token: str = Query(...)):
    """
    Called by the plugin on reconnect.  Returns any approval decisions that
    arrived while the plugin was offline and clears them from Firestore.
    """
    _require_device(device_token)
    db = get_db()
    col = db.collection("devices").document(device_token).collection("pending_approvals")
    docs = list(col.stream())
    items = []
    for doc in docs:
        items.append(doc.to_dict())
        doc.reference.delete()   # consume once delivered
    logger.info("[pending_approvals] device=%s delivered=%d", device_token[:8], len(items))
    return {"items": items}


@router.post("/approval", summary="Flutter approves/denies a QUESTION")
async def tunnel_approval(req: ApprovalRequest):
    """
    When the plugin emits a QUESTION signal, Flutter receives it via the
    monitor SSE stream and presents an approve/deny sheet.  The user's
    decision is POSTed here; we forward it back to the plugin's SSE queue
    so Hermes can continue (or abort) the task.
    """
    _require_device(req.device_token)

    # Build an A2A approval task and push it to the plugin queue
    approval_payload = {
        "type": "approval_response",
        "run_id": req.run_id,
        "approved": req.approved,
        "message": req.message,
        "timestamp": time.time(),
    }

    q = _plugin_queues.get(req.device_token)
    if q:
        await q.put(json.dumps({"event": "approval", "data": approval_payload}))
        logger.info(
            f"[approval] device={req.device_token[:8]}... run={req.run_id} approved={req.approved}"
        )
    else:
        # Plugin offline — persist in Firestore so it picks up on reconnect
        db = get_db()
        db.collection("devices").document(req.device_token) \
          .collection("pending_approvals").add(approval_payload)
        logger.warning(
            f"[approval] Plugin offline — stored in Firestore: run={req.run_id}"
        )

    # Also forward to any Flutter monitor subscribers (so the UI updates)
    run_map = _flutter_queues.get(req.device_token, {})
    flutter_q = run_map.get(req.run_id)
    if flutter_q:
        approval_event = {
            "signal": "APPROVAL_SENT",
            "run_id": req.run_id,
            "approved": req.approved,
        }
        await flutter_q.put(
            json.dumps({"event": "approval_sent", "data": approval_event})
        )

    return {"ok": True, "queued": q is not None}


@router.post("/dispatch", summary="Inject an A2A task to a connected plugin")
async def tunnel_dispatch(req: DispatchRequest):
    """
    Used by Herald Cloud (and tests) to push a task to a plugin.
    If online the task is enqueued on its SSE stream immediately.
    If offline it is stored in Firestore with status=queued.
    """
    _require_device(req.device_token)
    await dispatch_task(req.device_token, req.task)
    return {"ok": True, "task_id": req.task.task_id}
