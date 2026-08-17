"""
Herald Relay — SSE+POST tunnel client.

Replaces the old WebSocket relay_client. The plugin now dials the Herald Cloud
relay via three HTTP/SSE endpoints:

  POST /tunnel/connect  — register + send AgentCard
  GET  /tunnel/events   — long-lived SSE stream; Herald Cloud pushes A2A tasks
  POST /tunnel/update   — classified event updates back to Cloud

Reconnect: exponential back-off (1→2→4→8→30s, max 5 consecutive failures).
Hermes event forwarding: each SSE event from the local Hermes run is classified
via EventClassifier and POSTed to /tunnel/update.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import AsyncGenerator, Optional

import httpx

from .event_classifier import (
    EventClassifier, DONE, QUESTION, MILESTONE, IGNORE,
)
from .push_triggers import PushTriggerPolicy

logger = logging.getLogger(__name__)

# SSE line pattern
_SSE_DATA_RE = re.compile(r"^data:\s*(.*)")

# How long to wait for a heartbeat before considering the stream stale
_HEARTBEAT_TIMEOUT_S = 60.0

# How long the run-event stream may produce NOTHING before we declare the run
# stuck and tell the user. Hermes emits tool/message events continuously during
# real work, so several minutes of total silence means it is wedged, crashed,
# or the tunnel died. Without this the loop waits forever and the user hears
# nothing at all — the worst possible failure mode for a background task.
_STALL_TIMEOUT_S = float(os.environ.get("HERALD_STALL_TIMEOUT_S", "300"))

# How long to wait for the user to answer an approval before giving up.
# The old 300 s auto-denied any question asked while the user was asleep or
# away — the run died on its own timer. Approvals are re-pushed with high
# urgency, so a long window is safe and far friendlier.
_APPROVAL_TIMEOUT_S = float(os.environ.get("HERALD_APPROVAL_TIMEOUT_S", "3600"))


class HeraldRelayClient:
    """SSE+POST tunnel client that connects local Hermes to Herald Cloud.

    Public API:
        run_forever()       – main loop, call from asyncio.create_task()
        stop()              – graceful shutdown
        set_agent_card()    – update the AgentCard sent on connect/reconnect
    """

    def __init__(
        self,
        relay_url: str,
        device_token: str,
        local_hermes_url: str,
        hermes_version: str = "0.1.0",
        hermes_key: str = "",
    ):
        self.relay_url = relay_url.rstrip("/")
        self.device_token = device_token
        self.local_hermes_url = local_hermes_url.rstrip("/")
        self.hermes_version = hermes_version
        # Hermes api_server requires `Authorization: Bearer <API_S...Y>`;
        # without it every forwarded call comes back 401.
        self.hermes_key = hermes_key

        self._running = False
        self._agent_card: dict = {}
        self._hermes_session_id: Optional[str] = None
        # Decides which mid-run events are worth SPEAKING while the app is
        # closed. This class was fully implemented and unit-tested but had zero
        # production callers, so background progress never fired.
        self._push_policy = PushTriggerPolicy()

        # in-flight run tasks: run_id → asyncio.Task
        self._run_tasks: dict[str, asyncio.Task] = {}

        # approval queues: run_id → asyncio.Queue  (for QUESTION/approval flow)
        self._approval_queues: dict[str, asyncio.Queue] = {}

    def _hermes_headers(self) -> dict:
        """Auth headers for calls into the LOCAL Hermes api_server."""
        if self.hermes_key:
            return {"Authorization": f"Bearer {self.hermes_key}"}
        return {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_agent_card(self, card: dict) -> None:
        self._agent_card = card

    async def run_forever(self) -> None:
        """Main loop: connect to SSE tunnel, handle tasks, reconnect on drop."""
        self._running = True
        backoff = 1.0
        max_backoff = 30.0
        failures = 0

        while self._running:
            try:
                await self._connect_and_stream()
                backoff = 1.0  # reset on clean exit
                failures = 0
            except asyncio.CancelledError:
                logger.info("HeraldRelayClient: cancelled")
                break
            except Exception as exc:  # noqa: BLE001
                if not self._running:
                    break
                failures += 1
                logger.warning(
                    "Relay tunnel disconnected (attempt %d): %s. Reconnecting in %.0fs…",
                    failures,
                    exc,
                    backoff,
                )
                if failures >= 5:
                    logger.error("5 consecutive relay failures — backing off to 30s")
                    backoff = max_backoff
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def stop(self) -> None:
        self._running = False
        # Cancel any in-flight run tasks
        for task in self._run_tasks.values():
            task.cancel()
        self._run_tasks.clear()

    # ------------------------------------------------------------------
    # Core tunnel
    # ------------------------------------------------------------------

    async def _connect_and_stream(self) -> None:
        """Single tunnel session: POST /connect, then GET /events."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=_HEARTBEAT_TIMEOUT_S, write=10.0, pool=5.0),
        ) as client:
            # 1. Register with Cloud
            await self._register(client)

            # 2. Flush any offline-queued approvals from Firestore
            await self._flush_pending_approvals(client)

            # 3. Stream A2A tasks
            logger.info("Subscribed to tunnel events at %s/tunnel/events", self.relay_url)
            async for task_payload in self._event_stream(client):
                if not self._running:
                    break
                ptype = task_payload.get("type", "")
                if ptype == "task":
                    task = task_payload.get("task", {})
                    task_id = task.get("task_id", str(uuid.uuid4()))
                    command = task.get("command", "")
                    run_id = task.get("run_id") or str(uuid.uuid4())
                    logger.info("Received A2A task %s: %r", task_id, command[:80])
                    # Spawn run in background so we keep reading the SSE stream
                    t = asyncio.create_task(
                        self._execute_and_stream(client, task_id, run_id, command)
                    )
                    self._run_tasks[run_id] = t
                    t.add_done_callback(lambda ft, rid=run_id: self._run_tasks.pop(rid, None))
                elif ptype == "approval":
                    # Flutter approved/denied a QUESTION — unblock the waiting run
                    approval_data = task_payload.get("data", task_payload)
                    run_id = approval_data.get("run_id", "")
                    approved = approval_data.get("approved", False)
                    logger.info("Approval received: run=%s approved=%s", run_id, approved)
                    # Signal any waiting coroutine via the approval event queue
                    q = self._approval_queues.get(run_id)
                    if q:
                        await q.put({"approved": approved, "message": approval_data.get("message")})
                elif ptype == "forward_request":
                    # Cloud is proxying an HTTP call from the phone (/hermes/*)
                    # down to this machine's Hermes. Handle out-of-band so a slow
                    # request never stalls the event stream.
                    asyncio.create_task(
                        self._handle_forward_request(client, task_payload)
                    )

    async def _handle_forward_request(
        self, client: httpx.AsyncClient, payload: dict
    ) -> None:
        """Execute a proxied HTTP request against local Hermes and return the result.

        When the relay asks for ``stream: True`` (SSE endpoints such as
        ``/v1/runs/{id}/events``) the response is relayed CHUNK BY CHUNK as
        Hermes produces it. Buffering it into one reply meant a 75-second run
        delivered every spoken checkpoint in a single burst at the end — the
        user sat in silence for over a minute, which is precisely what
        checkpoints exist to prevent.
        """
        request_id = payload.get("request_id", "")
        method = (payload.get("method") or "GET").upper()
        path = payload.get("path") or "/"
        body = payload.get("body")
        wants_stream = bool(payload.get("stream"))

        url = f"{self.local_hermes_url}{path}"

        if wants_stream:
            await self._forward_streaming(client, request_id, method, url, body,
                                          path)
            return

        result: dict = {"device_token": self.device_token, "request_id": request_id}
        try:
            resp = await client.request(
                method,
                url,
                json=body if body is not None else None,
                headers=self._hermes_headers(),
                timeout=30.0,
            )
            try:
                parsed = resp.json()
            except Exception:
                parsed = {"raw": resp.text[:4000]}
            result["status"] = resp.status_code
            result["body"] = parsed
            logger.info("Forwarded %s %s -> %s", method, path, resp.status_code)
        except Exception as exc:  # noqa: BLE001
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Forward %s %s failed: %s", method, path, exc)

        try:
            await client.post(f"{self.relay_url}/tunnel/http_response", json=result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not return forwarded response: %s", exc)

    async def _forward_streaming(
        self,
        client: httpx.AsyncClient,
        request_id: str,
        method: str,
        url: str,
        body: dict | None,
        path: str,
    ) -> None:
        """Relay an SSE response upstream chunk-by-chunk, live."""
        endpoint = f"{self.relay_url}/tunnel/http_stream"
        sent = 0

        async def push(chunk: str = "", done: bool = False) -> None:
            try:
                await client.post(
                    endpoint,
                    json={
                        "device_token": self.device_token,
                        "request_id": request_id,
                        "chunk": chunk,
                        "done": done,
                    },
                    timeout=15.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream chunk push failed: %s", exc)

        try:
            headers = dict(self._hermes_headers())
            headers["Accept"] = "text/event-stream"
            async with client.stream(
                method,
                url,
                json=body if body is not None else None,
                headers=headers,
                # Long runs can span minutes; no total read cap.
                timeout=httpx.Timeout(None, connect=15.0),
            ) as resp:
                async for line in resp.aiter_lines():
                    if line is None:
                        continue
                    stripped = line.strip()
                    if not stripped:
                        continue
                    await push(stripped + "\n\n")
                    sent += 1
            logger.info("Streamed %s %s -> %d chunk(s)", method, path, sent)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Stream %s %s failed: %s", method, path, exc)
            await push(json.dumps({"error": f"{type(exc).__name__}: {exc}"}) )
        finally:
            await push(done=True)

    async def _register(self, client: httpx.AsyncClient) -> None:
        url = f"{self.relay_url}/tunnel/connect"
        payload = {
            "device_token": self.device_token,
            "agent_card": self._agent_card,
            "hermes_version": self.hermes_version,
        }
        resp = await client.post(url, json=payload)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Tunnel connect failed {resp.status_code}: {resp.text[:200]}"
            )
        logger.info("Tunnel registered: %s", resp.json())

    async def _flush_pending_approvals(self, client: httpx.AsyncClient) -> None:
        """
        On reconnect, fetch any approvals the user submitted while we were offline
        (stored in Firestore via POST /tunnel/approval when plugin was unreachable).
        Re-inject them into the local approval queues so in-flight runs can resume.
        """
        url = f"{self.relay_url}/tunnel/pending_approvals"
        try:
            resp = await client.get(url, params={"device_token": self.device_token})
            if resp.status_code == 200:
                items = resp.json().get("items", [])
                for item in items:
                    run_id = item.get("run_id", "")
                    approved = item.get("approved", False)
                    q = self._approval_queues.get(run_id)
                    if q:
                        await q.put({"approved": approved, "message": item.get("message")})
                        logger.info("Flushed offline approval: run=%s approved=%s", run_id, approved)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not flush pending approvals: %s", exc)

    async def _wait_for_approval(self, run_id: str, timeout: float = 300.0) -> dict:
        """
        Block until Flutter sends an approval for this run_id (or timeout).
        Returns {"approved": bool, "message": str|None}.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        self._approval_queues[run_id] = q
        try:
            return await asyncio.wait_for(q.get(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Approval timeout for run=%s — treating as denied", run_id)
            return {"approved": False, "message": "timeout"}
        finally:
            self._approval_queues.pop(run_id, None)

    async def _event_stream(
        self, client: httpx.AsyncClient
    ) -> AsyncGenerator[dict, None]:
        """Long-lived GET /tunnel/events SSE stream."""
        url = f"{self.relay_url}/tunnel/events"
        headers = {
            "Accept": "text/event-stream",
            "Cache-Control": "no-cache",
        }
        params = {"device_token": self.device_token}

        async with client.stream("GET", url, headers=headers, params=params) as resp:
            if resp.status_code != 200:
                text = await resp.aread()
                raise RuntimeError(
                    f"SSE stream failed {resp.status_code}: {text[:200]}"
                )

            async for line in resp.aiter_lines():
                if not self._running:
                    break
                if not line:
                    continue
                if line.startswith(": "):
                    # heartbeat comment
                    continue
                m = _SSE_DATA_RE.match(line)
                if m:
                    raw = m.group(1).strip()
                    if raw == "[DONE]":
                        return
                    try:
                        yield json.loads(raw)
                    except json.JSONDecodeError:
                        logger.debug("Non-JSON SSE data: %r", raw[:100])

    # ------------------------------------------------------------------
    # Hermes run execution + event forwarding
    # ------------------------------------------------------------------

    async def _execute_and_stream(
        self,
        client: httpx.AsyncClient,
        task_id: str,
        run_id: str,
        command: str,
    ) -> None:
        """Forward a command to local Hermes, classify events, POST updates back.

        Three properties matter here, because this is the path that runs while
        the user's app is closed:

        1. **Quiet by default.** Only signals worth a human's attention leave
           this loop. IGNORE is dropped entirely rather than POSTed.
        2. **Periodically audible on long runs.** ``PushTriggerPolicy`` decides
           when a MILESTONE is worth speaking, so an hour-long task is not
           silent for an hour.
        3. **Never silently dead.** A stall watchdog and a
           ``finally``-guaranteed terminal update mean a crashed/hung Hermes
           still reaches the user instead of failing closed.
        """
        classifier = EventClassifier()
        seq = 0
        start_time = time.monotonic()
        sent_terminal = False

        def _elapsed() -> float:
            return time.monotonic() - start_time

        try:
            # Ensure we have a Hermes session
            session_id = await self._ensure_session()

            # POST /v1/runs — pass the relay run_id as a hint so Hermes uses it
            run_resp = await self._hermes_post(
                f"{self.local_hermes_url}/v1/runs",
                {
                    "input": command,
                    "session_id": session_id,
                    "stream": True,
                    "run_id": run_id,  # hint — Hermes may or may not honour it
                },
            )
            # Always use the relay-assigned run_id for updates so Flutter monitor matches
            actual_run_id = run_id

            # Stream /v1/runs/{id}/events, under a stall watchdog. A bare
            # `async for` over a dead stream blocks forever with no output —
            # which is exactly the "app is dead and I never hear back" case.
            stream = self._hermes_sse(
                f"{self.local_hermes_url}/v1/runs/{actual_run_id}/events"
            ).__aiter__()

            while True:
                try:
                    raw_event = await asyncio.wait_for(
                        stream.__anext__(), timeout=_STALL_TIMEOUT_S
                    )
                except StopAsyncIteration:
                    # Stream ended WITHOUT a terminal event — Hermes died,
                    # was killed, or the tunnel dropped. Previously this exited
                    # silently and the user waited forever.
                    if not sent_terminal:
                        await self._post_update(client, {
                            "device_token": self.device_token,
                            "run_id": actual_run_id,
                            "seq": seq,
                            "signal": DONE,
                            "event": {"type": "run.failed", "data": {}},
                            "summary": "The task ended unexpectedly — Hermes "
                                       "stopped sending updates.",
                            "spoken_text": "Heads up — the task stopped "
                                           "unexpectedly before finishing.",
                        })
                        sent_terminal = True
                    break
                except asyncio.TimeoutError:
                    logger.warning(
                        "Hermes stream stalled >%.0fs for run=%s",
                        _STALL_TIMEOUT_S, actual_run_id,
                    )
                    await self._post_update(client, {
                        "device_token": self.device_token,
                        "run_id": actual_run_id,
                        "seq": seq,
                        "signal": DONE,
                        "event": {"type": "run.failed", "data": {"reason": "stalled"}},
                        "summary": (
                            f"No response from Hermes for "
                            f"{int(_STALL_TIMEOUT_S / 60)} minutes — the task "
                            f"may be stuck."
                        ),
                        "spoken_text": "Hermes has gone quiet and may be stuck. "
                                       "You might want to check on it.",
                    })
                    sent_terminal = True
                    break

                classified = classifier.feed(raw_event)

                # IGNORE is the token-stream firehose. Dropping it here is the
                # single biggest reduction in redundant chatter — it used to be
                # POSTed to the relay on every delta.
                if classified.signal == IGNORE:
                    continue

                update = {
                    "device_token": self.device_token,
                    "run_id": actual_run_id,
                    "seq": seq,
                    "signal": classified.signal,
                    "event": raw_event,
                }
                if classified.summary:
                    update["summary"] = classified.summary
                if classified.spoken_text:
                    update["spoken_text"] = classified.spoken_text

                # Should this one actually be SPOKEN while the app is closed?
                # The relay pushes QUESTION/DONE unconditionally; MILESTONE is
                # rate-limited here so a long run stays audible without
                # narrating every tool call into the user's pocket.
                if classified.signal == MILESTONE:
                    ok, text, _urgency = self._push_policy.should_push(
                        raw_event, _elapsed(), actual_run_id
                    )
                    if ok:
                        update["spoken_text"] = (
                            classified.summary or text or "Still working on it."
                        )

                await self._post_update(client, update)
                seq += 1

                # On QUESTION: pause and wait for Flutter approval
                if classified.signal == QUESTION:
                    logger.info("QUESTION signal — waiting for Flutter approval: run=%s", actual_run_id)
                    approval = await self._wait_for_approval(
                        actual_run_id, timeout=_APPROVAL_TIMEOUT_S
                    )
                    if not approval["approved"]:
                        # User denied (or never answered) — send a terminal
                        # update so Cloud and the user both learn the outcome.
                        timed_out = approval.get("message") == "timeout"
                        await self._post_update(client, {
                            "device_token": self.device_token,
                            "run_id": actual_run_id,
                            "seq": seq,
                            "signal": DONE,
                            "event": {"type": "approval_denied", "data": {}},
                            "summary": (
                                "Timed out waiting for your approval — the task "
                                "was not run."
                                if timed_out
                                else (approval.get("message")
                                      or "User denied the request.")
                            ),
                            "spoken_text": (
                                "I never heard back on that approval, so I "
                                "stopped the task."
                                if timed_out else None
                            ),
                        })
                        sent_terminal = True
                        return
                    # Approved — continue streaming (Hermes continues on its own)
                    logger.info("Approval granted: run=%s", actual_run_id)

                # After DONE we're finished
                if classified.signal == DONE:
                    sent_terminal = True
                    break

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("Error executing task %s: %s", task_id, exc)
            # Send an error DONE so Cloud isn't left waiting
            try:
                await self._post_update(client, {
                    "device_token": self.device_token,
                    "run_id": run_id,
                    "seq": seq,
                    "signal": DONE,
                    "event": {"type": "error", "data": {"message": str(exc)}},
                    "summary": f"Error: {str(exc)[:200]}",
                    "spoken_text": f"The task hit an error: {str(exc)[:150]}",
                })
                sent_terminal = True
            except Exception:
                pass
        finally:
            self._push_policy.forget(run_id)

    async def _ensure_session(self) -> str:
        """Lazily create a Hermes session and cache it."""
        if self._hermes_session_id:
            return self._hermes_session_id
        async with httpx.AsyncClient(timeout=10.0) as c:
            resp = await c.post(
                f"{self.local_hermes_url}/api/sessions",
                json={"metadata": {"source": "herald-relay-plugin"}},
            )
            if resp.status_code in (200, 201):
                self._hermes_session_id = resp.json().get("session_id", str(uuid.uuid4()))
            else:
                # Hermes may not require a session — use a stable random ID
                self._hermes_session_id = str(uuid.uuid4())
        return self._hermes_session_id  # type: ignore[return-value]

    async def _hermes_post(self, url: str, body: dict) -> dict:
        async with httpx.AsyncClient(timeout=30.0) as c:
            resp = await c.post(url, json=body)
            resp.raise_for_status()
            return resp.json()

    async def _hermes_sse(
        self, url: str
    ) -> AsyncGenerator[dict, None]:
        """Consume a Hermes SSE event stream."""
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=300.0, write=10.0, pool=5.0)
        ) as c:
            async with c.stream("GET", url, headers={"Accept": "text/event-stream"}) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith(":"):
                        continue
                    m = _SSE_DATA_RE.match(line)
                    if m:
                        raw = m.group(1).strip()
                        if raw == "[DONE]":
                            return
                        try:
                            yield json.loads(raw)
                        except json.JSONDecodeError:
                            pass

    async def _post_update(self, client: httpx.AsyncClient, update: dict) -> None:
        url = f"{self.relay_url}/tunnel/update"
        try:
            resp = await client.post(url, json=update, timeout=10.0)
            if resp.status_code not in (200, 201):
                logger.warning("tunnel/update %d: %s", resp.status_code, resp.text[:100])
        except httpx.HTTPError as exc:
            logger.warning("Failed to post tunnel update: %s", exc)
