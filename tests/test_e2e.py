"""
End-to-end test for Herald Relay system.

Tests the full flow:
1. Start relay server (uvicorn on port 19876)
2. Plugin connects via WS /relay/connect and registers
3. Register device_token with FCM stub via POST /push/register
4. HTTP client calls POST /hermes/v1/runs — relayed to plugin WS
5. Plugin receives forward_request and sends back forward_response
6. HTTP response verified
7. POST /push/send verified (FCM stub)
8. GET /health shows connected_devices: 1
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time

import httpx
import pytest
import websockets

BASE_URL = "http://localhost:19876"
WS_URL = "ws://localhost:19876/relay/connect"
DEVICE_TOKEN = "e2e-test-device-token-abc123"
SERVER_PORT = 19876


def wait_for_server(timeout: float = 15.0) -> bool:
    """Poll until the server is up or timeout."""
    import urllib.request
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(f"http://localhost:{SERVER_PORT}/health", timeout=1)
            return True
        except Exception:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def relay_server():
    """Start uvicorn relay server in a subprocess for the duration of the test module."""
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode()

    env = {
        **os.environ,
        "ENCRYPTION_KEY": key,
        "HERALD_RELAY_URL": f"http://localhost:{SERVER_PORT}",
        "FCM_PROJECT_ID": "",  # force stub mode
    }

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn",
            "main:app",
            "--host", "0.0.0.0",
            "--port", str(SERVER_PORT),
            "--log-level", "warning",
        ],
        cwd=os.path.join(os.path.dirname(__file__), "..", "backend"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    ready = wait_for_server(15.0)
    if not ready:
        proc.kill()
        out, err = proc.communicate(timeout=5)
        raise RuntimeError(
            f"Relay server failed to start.\nstdout: {out.decode()}\nstderr: {err.decode()}"
        )

    yield proc

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.mark.asyncio
async def test_full_e2e(relay_server):
    """Full end-to-end Herald Relay test."""

    # -----------------------------------------------------------------------
    # 1. Plugin connects via WebSocket
    # -----------------------------------------------------------------------
    async with websockets.connect(WS_URL) as ws:
        # Send register message
        register_msg = {
            "type": "register",
            "device_token": DEVICE_TOKEN,
            "hermes_version": "0.1.0-test",
        }
        await ws.send(json.dumps(register_msg))

        # Receive 'registered' ack
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        ack = json.loads(raw)
        assert ack["type"] == "registered", f"Expected registered, got: {ack}"
        relay_id = ack["relay_id"]
        assert relay_id, "relay_id should be non-empty"

        # -----------------------------------------------------------------------
        # 2. Register device_token with FCM stub
        # -----------------------------------------------------------------------
        async with httpx.AsyncClient(base_url=BASE_URL) as http:
            resp = await http.post("/push/register", json={
                "device_token": DEVICE_TOKEN,
                "fcm_token": "stub-fcm-token-abc123",
                "platform": "android",
                "plan": "byok",
            })
            assert resp.status_code == 200, f"push/register failed: {resp.text}"
            reg_data = resp.json()
            assert reg_data["device_token"] == DEVICE_TOKEN

        # -----------------------------------------------------------------------
        # 3. Check /health shows connected_devices: 1
        # -----------------------------------------------------------------------
        async with httpx.AsyncClient(base_url=BASE_URL) as http:
            resp = await http.get("/health")
            assert resp.status_code == 200
            health = resp.json()
            assert health["connected_devices"] >= 1, f"Expected >=1 connected device: {health}"

        # -----------------------------------------------------------------------
        # 4. HTTP client calls POST /hermes/v1/runs
        #    Plugin must receive forward_request and respond
        # -----------------------------------------------------------------------

        run_body = {"input": "hello from test", "model": "test-model"}

        async def plugin_handler():
            """Listen for forward_request and send back forward_response."""
            for _ in range(10):
                raw_msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                msg = json.loads(raw_msg)
                if msg["type"] == "forward_request":
                    # Send forward_response
                    response = {
                        "type": "forward_response",
                        "request_id": msg["request_id"],
                        "status": 200,
                        "body": {"run_id": "run-test-42", "status": "queued"},
                        "headers": {},
                    }
                    await ws.send(json.dumps(response))
                    return msg
                elif msg["type"] == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                    continue
            raise AssertionError("Never received forward_request")

        async def http_caller():
            """Call POST /hermes/v1/runs and collect SSE response."""
            async with httpx.AsyncClient(base_url=BASE_URL, timeout=15.0) as http:
                chunks = []
                async with http.stream(
                    "POST",
                    "/hermes/v1/runs",
                    json=run_body,
                    headers={"X-Device-Token": DEVICE_TOKEN},
                ) as response:
                    assert response.status_code == 200, f"Expected 200: {response.status_code}"
                    async for line in response.aiter_lines():
                        if line:
                            chunks.append(line)
                return chunks

        # Run both concurrently
        results = await asyncio.gather(plugin_handler(), http_caller())
        forward_req_msg, sse_chunks = results

        # -----------------------------------------------------------------------
        # 5. Verify the forwarded request and response
        # -----------------------------------------------------------------------
        assert forward_req_msg["method"] == "POST"
        assert forward_req_msg["path"] == "/v1/runs"
        assert forward_req_msg["body"] == run_body

        # SSE chunks should contain the response body
        assert len(sse_chunks) > 0, "Expected at least one SSE chunk"
        data_lines = [c for c in sse_chunks if c.startswith("data:")]
        assert len(data_lines) > 0, f"Expected data: lines in SSE, got: {sse_chunks}"
        payload = json.loads(data_lines[0].removeprefix("data:").strip())
        assert payload.get("run_id") == "run-test-42", f"Unexpected SSE payload: {payload}"

        # -----------------------------------------------------------------------
        # 6. POST /push/send — verify FCM stub handles it
        # -----------------------------------------------------------------------
        async with httpx.AsyncClient(base_url=BASE_URL) as http:
            resp = await http.post("/push/send", json={
                "device_token": DEVICE_TOKEN,
                "message": "Test push notification",
                "urgency": "high",
                "metadata": {"relay_id": relay_id},
            })
            assert resp.status_code == 200, f"push/send failed: {resp.text}"
            push_data = resp.json()
            assert push_data.get("ok") is True
            assert push_data.get("stub") is True, "Expected FCM stub mode (no real FCM project)"

        # -----------------------------------------------------------------------
        # 7. Final health check (device still connected inside WS context)
        # -----------------------------------------------------------------------
        async with httpx.AsyncClient(base_url=BASE_URL) as http:
            resp = await http.get("/health")
            assert resp.status_code == 200
            health = resp.json()
            assert health["status"] == "ok"
            assert health["connected_devices"] >= 1

    # After WS context closes, device should be unregistered
    await asyncio.sleep(0.5)
    async with httpx.AsyncClient(base_url=BASE_URL) as http:
        resp = await http.get("/health")
        health = resp.json()
        assert health["connected_devices"] == 0, f"Expected 0 after disconnect: {health}"
