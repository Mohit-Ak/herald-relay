"""The relay must carry the CALLER's Authorization to the caller's local Hermes.

WHY THIS FILE EXISTS
--------------------
Hermes multiplexes profiles on a single port (``/p/<profile>/**``) and every
named profile has its OWN ``API_SERVER_KEY``, failing CLOSED on a mismatch. The
tunnel client knows only the DEFAULT profile's key.

So when the relay dropped the inbound ``Authorization`` header, the plugin
stamped the default key onto every forwarded call and each named profile
answered ``401 Invalid gateway API key`` *through the relay* while the identical
request succeeded on the LAN. The installer's QR code promised "one scan adds
all 6 agents" and delivered one working agent out of six.

These tests pin both halves of the contract:
  1. the relay forwards the caller's Authorization (and the X-Hermes-Key alias),
  2. the plugin prefers a forwarded header over its own local key, and still
     falls back to that local key when an older relay sends nothing.
"""
from __future__ import annotations

import pytest
from httpx import AsyncClient, ASGITransport


@pytest.fixture
def app():
    from main import app
    return app


async def _register_and_connect(c) -> str:
    """Register a device and mark its tunnel as connected."""
    from routers import tunnel as tunnel_mod
    import asyncio

    reg = await c.post("/push/register", json={"fcm_token": "tok-auth-fwd"})
    token = reg.json()["device_token"]
    tunnel_mod._plugin_queues[token] = asyncio.Queue()
    return token


# ── relay half ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_device_scoped_forwards_caller_authorization(app, monkeypatch):
    """A per-profile bearer token must reach the plugin untouched."""
    from routers import tunnel as tunnel_mod

    seen: dict = {}

    async def fake_forward_http(token, method, path, body=None, headers=None, **kw):
        seen["path"] = path
        seen["headers"] = headers or {}
        return {"status": 200, "body": {"ok": True}, "headers": {}}

    monkeypatch.setattr(tunnel_mod, "forward_http", fake_forward_http)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        token = await _register_and_connect(c)
        resp = await c.get(
            f"/hermes/d/{token}/p/dobbo/api/soul",
            headers={"Authorization": "Bearer dobbo-profile-key"},
        )

    assert resp.status_code == 200
    assert seen["path"] == "/p/dobbo/api/soul"
    assert seen["headers"].get("Authorization") == "Bearer dobbo-profile-key"


@pytest.mark.asyncio
async def test_x_hermes_key_alias_is_promoted_to_bearer(app, monkeypatch):
    """Clients that only speak X-Hermes-Key must still authenticate."""
    from routers import tunnel as tunnel_mod

    seen: dict = {}

    async def fake_forward_http(token, method, path, body=None, headers=None, **kw):
        seen["headers"] = headers or {}
        return {"status": 200, "body": {}, "headers": {}}

    monkeypatch.setattr(tunnel_mod, "forward_http", fake_forward_http)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        token = await _register_and_connect(c)
        resp = await c.get(
            f"/hermes/d/{token}/p/quark/api/soul",
            headers={"X-Hermes-Key": "quark-profile-key"},
        )

    assert resp.status_code == 200
    assert seen["headers"].get("Authorization") == "Bearer quark-profile-key"


@pytest.mark.asyncio
async def test_hop_by_hop_headers_are_not_forwarded(app, monkeypatch):
    """Only the auth allowlist travels — a stale Content-Length corrupts the
    request the plugin rebuilds."""
    from routers import tunnel as tunnel_mod

    seen: dict = {}

    async def fake_forward_http(token, method, path, body=None, headers=None, **kw):
        seen["headers"] = headers or {}
        return {"status": 200, "body": {}, "headers": {}}

    monkeypatch.setattr(tunnel_mod, "forward_http", fake_forward_http)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        token = await _register_and_connect(c)
        await c.get(
            f"/hermes/d/{token}/api/soul",
            headers={
                "Authorization": "Bearer default-key",
                "Host": "evil.example",
                "Connection": "keep-alive",
                "User-Agent": "herald-test",
            },
        )

    assert set(seen["headers"]) == {"Authorization"}


@pytest.mark.asyncio
async def test_generic_passthrough_forwards_authorization(app, monkeypatch):
    """The catch-all route is the one Herald actually uses for most paths."""
    from routers import tunnel as tunnel_mod

    seen: dict = {}

    async def fake_forward_http(token, method, path, body=None, headers=None, **kw):
        seen["headers"] = headers or {}
        return {"status": 200, "body": {}, "headers": {}}

    monkeypatch.setattr(tunnel_mod, "forward_http", fake_forward_http)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        token = await _register_and_connect(c)
        resp = await c.get(
            "/hermes/p/eh-ecommerce/api/soul",
            headers={
                "X-Device-Token": token,
                "Authorization": "Bearer ecommerce-key",
            },
        )

    assert resp.status_code == 200
    assert seen["headers"].get("Authorization") == "Bearer ecommerce-key"


# ── plugin half ─────────────────────────────────────────────────────────────

def _client():
    import sys
    from pathlib import Path

    plugin_root = Path(__file__).resolve().parents[2] / "plugin"
    if str(plugin_root) not in sys.path:
        sys.path.insert(0, str(plugin_root))
    from herald_relay.relay_client import HeraldRelayClient

    return HeraldRelayClient(
        relay_url="http://relay.test",
        device_token="dev-token",
        local_hermes_url="http://127.0.0.1:8642",
        hermes_key="default-profile-key",
    )


def test_plugin_prefers_forwarded_authorization():
    c = _client()
    headers = c._hermes_headers({"Authorization": "Bearer dobbo-profile-key"})
    assert headers == {"Authorization": "Bearer dobbo-profile-key"}


def test_plugin_falls_back_to_local_key_when_nothing_forwarded():
    """An older relay forwards no headers — the default profile must still work."""
    c = _client()
    assert c._hermes_headers({}) == {"Authorization": "Bearer default-profile-key"}
    assert c._hermes_headers(None) == {"Authorization": "Bearer default-profile-key"}


def test_plugin_ignores_non_auth_forwarded_headers():
    c = _client()
    headers = c._hermes_headers({"Accept": "application/json"})
    assert headers == {"Authorization": "Bearer default-profile-key"}


def test_plugin_forward_request_passes_headers_through(monkeypatch):
    """_handle_forward_request must actually read payload['headers']."""
    import asyncio

    c = _client()
    captured: dict = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class FakeClient:
        async def request(self, method, url, **kw):
            captured["headers"] = kw.get("headers")
            return FakeResp()

        async def post(self, *a, **kw):
            return FakeResp()

    asyncio.run(
        c._handle_forward_request(
            FakeClient(),
            {
                "request_id": "r1",
                "method": "GET",
                "path": "/p/dobbo/api/soul",
                "headers": {"Authorization": "Bearer dobbo-profile-key"},
            },
        )
    )
    assert captured["headers"]["Authorization"] == "Bearer dobbo-profile-key"
