"""
End-to-end test for the Herald Relay pipeline (no external deps):

  Flow:
    1. Start relay server (uvicorn on 19876)
    2. Start mock-Hermes (uvicorn on 19877) — always returns a tool_end then final
    3. Register device_token via POST /push/register
    4. Plugin connects: POST /tunnel/connect
    5. POST /tunnel/dispatch — injects an A2A task into the relay
    6. Plugin polls /tunnel/events (SSE) and picks up the task
    7. Plugin posts run to mock-Hermes → gets SSE events back
    8. Plugin POSTs classified updates to /tunnel/update
    9. Flutter monitor: GET /monitor/{token}/{run_id} receives DONE via SSE
   10. Assert final signal == DONE received by monitor within 15s
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest

RELAY_PORT = 19876
MOCK_HERMES_PORT = 19877
BASE_RELAY = f"http://localhost:{RELAY_PORT}"
BASE_HERMES = f"http://localhost:{MOCK_HERMES_PORT}"
PLUGIN_DIR = Path(__file__).parent.parent / "plugin"
BACKEND_DIR = Path(__file__).parent.parent / "backend"

# ── helpers ────────────────────────────────────────────────────────────────

def _poll_until(url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=1)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


def _make_mock_hermes_app():
    """Tiny FastAPI app that mimics enough of the Hermes API for tests."""
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    import asyncio

    app = FastAPI()
    _run_ids: dict = {}

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/v1/runs")
    async def start_run(body: dict = None):
        run_id = str(uuid.uuid4())
        return {"run_id": run_id, "status": "running"}

    @app.get("/v1/runs/{run_id}/events")
    async def run_events(run_id: str):
        async def gen():
            # emit tool_end then final
            yield f"data: {json.dumps({'type': 'tool_end', 'data': {'name': 'read_file'}})}\n\n"
            await asyncio.sleep(0.05)
            yield f"data: {json.dumps({'type': 'final', 'data': {'summary': 'All done via mock.'}})}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/sessions")
    async def create_session():
        return {"session_id": str(uuid.uuid4())}

    return app


# ── fixtures ───────────────────────────────────────────────────────────────

def _start_relay():
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()
    env = {
        **os.environ,
        "ENCRYPTION_KEY": key,
        "HERALD_RELAY_URL": BASE_RELAY,
        "FCM_PROJECT_ID": "",  # FCM stub mode (no real push)
        "HERALD_INMEMORY_DB": "1",  # Firestore stub — hermetic, no GCP creds
        "PORT": str(RELAY_PORT),
    }
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", str(RELAY_PORT),
            "--log-level", "warning",
        ],
        cwd=str(BACKEND_DIR),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _start_mock_hermes():
    """Write a tiny server file and launch it."""
    mock_src = Path("/tmp/mock_hermes_server.py")
    mock_src.write_text(
        """
import uuid, json, asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
import uvicorn

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/v1/runs")
async def start_run(body: dict = None):
    return {"run_id": str(uuid.uuid4())}

@app.get("/v1/runs/{run_id}/events")
async def events(run_id: str):
    async def gen():
        yield "data: " + json.dumps({"type": "tool_end", "data": {"name": "read_file"}}) + "\\n\\n"
        await asyncio.sleep(0.05)
        yield "data: " + json.dumps({"type": "final", "data": {"summary": "Mock done."}}) + "\\n\\n"
        yield "data: [DONE]\\n\\n"
    return StreamingResponse(gen(), media_type="text/event-stream")

@app.post("/api/sessions")
async def sessions():
    return {"session_id": str(uuid.uuid4())}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=19877, log_level="warning")
"""
    )
    proc = subprocess.Popen(
        [sys.executable, str(mock_src)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


@pytest.fixture(scope="module")
def servers():
    relay_proc = _start_relay()
    hermes_proc = _start_mock_hermes()

    assert _poll_until(f"{BASE_RELAY}/health", 15), "Relay server failed to start"
    assert _poll_until(f"{BASE_HERMES}/health", 10), "Mock Hermes failed to start"

    yield

    relay_proc.terminate()
    hermes_proc.terminate()
    try:
        relay_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        relay_proc.kill()
    try:
        hermes_proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        hermes_proc.kill()


# ── the e2e test ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_full_pipeline(servers):
    """
    Full pipeline: register → plugin connects → relay dispatches task →
    plugin runs mock-Hermes → classifies → updates relay → Flutter monitor
    receives DONE.
    """
    import sys
    sys.path.insert(0, str(PLUGIN_DIR))
    from herald_relay.relay_client import HeraldRelayClient

    async with httpx.AsyncClient(timeout=10.0) as http:
        # 1. Register device
        reg = await http.post(f"{BASE_RELAY}/push/register", json={"fcm_token": "e2e-fcm"})
        assert reg.status_code == 200, reg.text
        token = reg.json()["device_token"]

        # 2. Plugin connect
        conn = await http.post(f"{BASE_RELAY}/tunnel/connect", json={
            "device_token": token,
            "agent_card": {"name": "E2EAgent"},
            "hermes_version": "0.1.0-e2e",
        })
        assert conn.status_code == 200, conn.text

    # 3. Start the relay plugin client in background
    client = HeraldRelayClient(
        relay_url=BASE_RELAY,
        device_token=token,
        local_hermes_url=BASE_HERMES,
    )
    plugin_task = asyncio.create_task(client.run_forever())

    try:
        # Give plugin more time to subscribe to SSE stream
        await asyncio.sleep(1.5)

        run_id = str(uuid.uuid4())
        task_id = str(uuid.uuid4())
        import datetime

        # 4. Dispatch an A2A task into the relay (simulate Cloud→Plugin)
        async with httpx.AsyncClient(timeout=10.0) as http:
            dispatch = await http.post(f"{BASE_RELAY}/tunnel/dispatch", json={
                "device_token": token,
                "task": {
                    "task_id": task_id,
                    "device_token": token,
                    "run_id": run_id,
                    "command": "echo hello world",
                    "created_at": datetime.datetime.utcnow().isoformat(),
                    "status": "pending",
                },
            })
            # 202 accepted or 200
            assert dispatch.status_code in (200, 202), dispatch.text

        # 5. Monitor for DONE on the Flutter SSE endpoint
        done_received = asyncio.Event()

        async def _monitor():
            url = f"{BASE_RELAY}/monitor/{token}/{run_id}"
            deadline = time.monotonic() + 12.0
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, read=15)) as mc:
                async with mc.stream("GET", url) as resp:
                    async for line in resp.aiter_lines():
                        if time.monotonic() > deadline:
                            break
                        if not line or line.startswith(":"):
                            continue
                        if line.startswith("data:"):
                            raw = line[5:].strip()
                            if raw == "[DONE]":
                                break
                            try:
                                ev = json.loads(raw)
                                if ev.get("signal") == "DONE":
                                    done_received.set()
                                    return
                            except Exception:
                                pass

        monitor_task = asyncio.create_task(_monitor())
        # Wait up to 12s for DONE
        try:
            await asyncio.wait_for(done_received.wait(), timeout=12.0)
        finally:
            monitor_task.cancel()
            try:
                await monitor_task
            except (asyncio.CancelledError, Exception):
                pass

        assert done_received.is_set(), "Flutter monitor never received DONE signal within 12s"

    finally:
        await client.stop()
        plugin_task.cancel()
        try:
            await plugin_task
        except (asyncio.CancelledError, Exception):
            pass
