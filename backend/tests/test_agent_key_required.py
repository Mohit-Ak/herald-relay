"""A device's relay address must not be enough on its own to reach its agent.

WHY THIS FILE EXISTS
--------------------
The tunnel client on the agent's machine adds that machine's own API key to
any forwarded request that carries none (kept so an older relay that dropped
headers still worked). The relay passed keyless requests straight through, so
anyone holding only the address ``/hermes/d/<device_token>`` could list
sessions and start runs: verified on a real agent, where no key and the right
key got the same 200 while a wrong key got 401.

The fix is at the relay, where it reaches every installed machine at once:
a request with no key is refused before it enters the tunnel. Only the
liveness checks stay open; they carry no agent data.
"""
from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

KEY = {"Authorization": "Bearer the-agent-key"}


@pytest.fixture
def app():
    from main import app
    return app


@pytest.fixture
def tunnel(monkeypatch):
    """A connected device whose tunnel records what reached it."""
    from routers import tunnel as tunnel_mod

    seen: list[dict] = []

    async def fake_forward_http(token, method, path, body=None, headers=None, **kw):
        seen.append({"path": path, "headers": headers or {}})
        return {"status": 200, "body": {"ok": True}, "headers": {}}

    async def fake_stream(token, method, path, body=None, headers=None, **kw):
        seen.append({"path": path, "headers": headers or {}})
        yield {"type": "chunk", "data": "data: {}\n\n"}

    monkeypatch.setattr(tunnel_mod, "forward_http", fake_forward_http)
    for name in ("open_stream",):
        if hasattr(tunnel_mod, name):
            monkeypatch.setattr(tunnel_mod, name, fake_stream)
    token = "dev-token-for-key-tests-0001"
    tunnel_mod._plugin_queues[token] = asyncio.Queue()
    yield token, seen
    tunnel_mod._plugin_queues.pop(token, None)


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


KEYLESS = [
    ("GET", "/hermes/d/{t}/v1/models"),
    ("GET", "/hermes/d/{t}/api/sessions?limit=1"),
    ("POST", "/hermes/d/{t}/api/sessions"),
    ("POST", "/hermes/d/{t}/v1/runs"),
    ("GET", "/hermes/d/{t}/v1/runs/r1/events"),
    ("GET", "/hermes/d/{t}/p/quark/api/soul"),
    ("GET", "/hermes/v1/models"),
    ("GET", "/hermes/v1/channels"),
    ("POST", "/hermes/v1/runs"),
    ("GET", "/hermes/v1/runs/r1/events"),
    ("POST", "/hermes/v1/runs/r1/stop"),
    ("POST", "/hermes/v1/runs/r1/approval"),
    ("GET", "/hermes/api/sessions"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path", KEYLESS)
async def test_a_request_with_no_key_never_enters_the_tunnel(app, tunnel, method, path):
    token, seen = tunnel
    async with _client(app) as c:
        r = await c.request(method, path.format(t=token), json={} if method == "POST" else None)
    assert r.status_code == 401, (path, r.status_code)
    assert r.json()["detail"]["error"] == "agent_key_required"
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [
    {"Authorization": ""}, {"Authorization": "Bearer"}, {"Authorization": "Bearer   "},
    {"X-Hermes-Key": ""}, {"X-Hermes-Key": "   "},
])
async def test_an_empty_key_counts_as_no_key(app, tunnel, headers):
    token, seen = tunnel
    async with _client(app) as c:
        r = await c.get(f"/hermes/d/{token}/v1/models", headers=headers)
    assert r.status_code == 401 and seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [KEY, {"X-Hermes-Key": "the-agent-key"}])
async def test_a_request_with_a_key_goes_through_unchanged(app, tunnel, headers):
    token, seen = tunnel
    async with _client(app) as c:
        r = await c.get(f"/hermes/d/{token}/v1/models", headers=headers)
    assert r.status_code == 200
    assert seen[0]["headers"]["Authorization"] == "Bearer the-agent-key"


@pytest.mark.asyncio
async def test_the_relay_does_not_judge_the_key_the_agent_does(app, tunnel):
    """Per-profile keys differ, so the relay only requires that one is sent."""
    token, seen = tunnel
    async with _client(app) as c:
        r = await c.get(f"/hermes/d/{token}/p/quark/api/soul",
                        headers={"Authorization": "Bearer some-other-profile-key"})
    assert r.status_code == 200 and len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    "/hermes/d/{t}/health",
    "/hermes/health?device_token={t}",
    "/hermes/health/detailed?device_token={t}",
])
async def test_liveness_checks_stay_open(app, tunnel, path):
    """`herald-tunnel status`, the app's connected dot and our own wake check
    ask only "is the tunnel up" and send no key."""
    token, _ = tunnel
    async with _client(app) as c:
        r = await c.get(path.format(t=token))
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_an_offline_device_still_says_offline_not_unauthorised(app):
    """A sleeping cloud machine must read as offline so the backend wakes it."""
    async with _client(app) as c:
        r = await c.get("/hermes/d/nobody-is-connected-here-0001/v1/models")
    assert r.status_code == 503
    assert r.json()["detail"]["error"] == "device_offline"


@pytest.mark.asyncio
async def test_the_switch_restores_the_old_behaviour(app, tunnel, monkeypatch):
    monkeypatch.setenv("HERALD_REQUIRE_AGENT_KEY", "0")
    token, seen = tunnel
    async with _client(app) as c:
        r = await c.get(f"/hermes/d/{token}/v1/models")
    assert r.status_code == 200 and len(seen) == 1
