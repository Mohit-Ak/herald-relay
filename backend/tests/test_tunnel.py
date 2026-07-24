"""
Tests for the SSE tunnel endpoints: /tunnel/connect, /tunnel/events, /tunnel/update,
and /monitor/{device_token}/{run_id}.

Uses the FakeDB fixture from tests/conftest.py (autouse).
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app():
    from main import app
    return app


# ── /tunnel/connect ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tunnel_connect_unknown_device(app):
    """connect with unregistered device_token → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/tunnel/connect", json={
            "device_token": "ghost-token",
            "agent_card": {},
            "hermes_version": "0.1.0",
        })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tunnel_connect_registered_device(app):
    """Registered device can connect and gets tunnel_url back."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        # First register the device via push/register
        reg = await c.post("/push/register", json={"fcm_token": "tok123"})
        token = reg.json()["device_token"]

        resp = await c.post("/tunnel/connect", json={
            "device_token": token,
            "agent_card": {"name": "TestAgent", "version": "0.1"},
            "hermes_version": "0.1.0",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "tunnel_url" in data


# ── /tunnel/update ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tunnel_update_unknown_device(app):
    """Update from unregistered device → 404."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        resp = await c.post("/tunnel/update", json={
            "device_token": "ghost",
            "run_id": str(uuid.uuid4()),
            "seq": 0,
            "signal": "IGNORE",
            "event": {"type": "partial"},
        })
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_tunnel_update_accumulate(app):
    """ACCUMULATE signal stores update in Firestore and returns ok."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        reg = await c.post("/push/register", json={"fcm_token": "fcmX"})
        token = reg.json()["device_token"]
        run_id = str(uuid.uuid4())

        resp = await c.post("/tunnel/update", json={
            "device_token": token,
            "run_id": run_id,
            "seq": 0,
            "signal": "ACCUMULATE",
            "event": {"type": "tool_end", "data": {"name": "read_file"}},
            "summary": "Ran read_file.",
        })
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_tunnel_update_done_no_fcm_project(app):
    """DONE signal with no FCM_PROJECT_ID → stub push (no error)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        reg = await c.post("/push/register", json={"fcm_token": "fcmDone"})
        token = reg.json()["device_token"]
        run_id = str(uuid.uuid4())

        resp = await c.post("/tunnel/update", json={
            "device_token": token,
            "run_id": run_id,
            "seq": 0,
            "signal": "DONE",
            "event": {"type": "final", "data": {}},
            "summary": "All done.",
            "spoken_text": "Done.",
        })
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_tunnel_update_question_no_fcm_project(app):
    """QUESTION signal triggers push (stub) — returns ok."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        reg = await c.post("/push/register", json={"fcm_token": "fcmQ"})
        token = reg.json()["device_token"]
        run_id = str(uuid.uuid4())

        resp = await c.post("/tunnel/update", json={
            "device_token": token,
            "run_id": run_id,
            "seq": 0,
            "signal": "QUESTION",
            "event": {"type": "approval_required", "data": {"prompt": "Delete prod?"}},
            "spoken_text": "Delete prod?",
        })
    assert resp.status_code == 200


# ── dispatch_task helper ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dispatch_task_offline_device(app):
    """dispatch_task for offline device stores in Firestore with status=queued."""
    from routers.tunnel import dispatch_task, A2ATask
    import datetime

    task = A2ATask(
        task_id=str(uuid.uuid4()),
        device_token="offline-device",
        command="echo hello",
        created_at=datetime.datetime.utcnow().isoformat(),
    )
    # Should not raise even when device is offline (stores in Firestore)
    await dispatch_task("offline-device", task)
    # FakeDB has no validator — just check it doesn't blow up


# ── Event classifier isolation tests ───────────────────────────────────────
# These run via pytest in the backend dir; they import the classifier
# directly from the plugin package which must be installed or on PYTHONPATH.
# When running `pytest` from the repo root or with the plugin installed this
# works automatically. If running only from backend/, set PYTHONPATH to
# ../../plugin before running.
#
# The classifier tests live separately in plugin/tests/test_classifier.py —
# skipped here to avoid import path issues.
