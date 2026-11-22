"""Herald Bridge releases on the relay (herald#505): the signed manifest is
served as-is, /tunnel/connect records plugin_version and hints the latest
release, and the ops endpoints stay dark without HERALD_RELAY_OPS_TOKEN."""
from __future__ import annotations

import base64
import json
import time

import pytest
from httpx import ASGITransport, AsyncClient


def _envelope(version="0.8.1", min_supported="0.7.0", rollout=10) -> dict:
    m = {"package": "hermes-herald", "version": version, "min_supported": min_supported,
         "rollout_percent": rollout}
    return {"payloadType": "application/vnd.herald.bridge-manifest+json",
            "payload": base64.b64encode(json.dumps(m).encode()).decode(),
            "signatures": [{"keyid": "k", "sig": "c2ln"}]}


@pytest.fixture
def app():
    from main import app
    return app


@pytest.fixture
def manifest_file(tmp_path, monkeypatch):
    p = tmp_path / "plugin-manifest.json"
    p.write_text(json.dumps(_envelope()))
    monkeypatch.setenv("HERALD_PLUGIN_MANIFEST", str(p))
    return p


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _device(token: str, **fields):
    from services.firestore_client import get_db
    get_db().collection("devices").document(token).set(
        {"device_token": token, "created_at": time.time(), "last_seen": time.time(), **fields})


@pytest.mark.asyncio
async def test_manifest_404_without_a_release(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HERALD_PLUGIN_MANIFEST", str(tmp_path / "missing.json"))
    async with _client(app) as c:
        r = await c.get("/relay/plugin/manifest")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_manifest_served_unchanged(app, manifest_file, monkeypatch):
    monkeypatch.delenv("MIN_PLUGIN_VERSION", raising=False)
    async with _client(app) as c:
        r = await c.get("/relay/plugin/manifest")
    assert r.status_code == 200
    j = r.json()
    assert j["envelope"] == json.loads(manifest_file.read_text())  # never re-signed here
    assert j["latest"] == "0.8.1" and j["min_plugin_version"] == "0.7.0"
    assert "max-age" in r.headers["cache-control"]


@pytest.mark.asyncio
async def test_min_plugin_version_env_wins(app, manifest_file, monkeypatch):
    monkeypatch.setenv("MIN_PLUGIN_VERSION", "0.8.0")
    async with _client(app) as c:
        j = (await c.get("/relay/plugin/manifest")).json()
    assert j["min_plugin_version"] == "0.8.0"


@pytest.mark.asyncio
async def test_connect_records_plugin_version_and_hints(app, manifest_file):
    from services.firestore_client import get_db
    _device("dev-505")
    async with _client(app) as c:
        r = await c.post("/tunnel/connect", json={
            "device_token": "dev-505", "agent_card": {}, "hermes_version": "x",
            "plugin_version": "0.8.0"})
    assert r.status_code == 200
    assert r.json()["plugin"] == {"latest": "0.8.1", "min_supported": "0.7.0"}
    d = get_db().collection("devices").document("dev-505").get().to_dict()
    assert d["plugin_version"] == "0.8.0" and d["plugin_version_at"] > 0


@pytest.mark.asyncio
async def test_connect_from_old_plugin_still_works(app, tmp_path, monkeypatch):
    monkeypatch.setenv("HERALD_PLUGIN_MANIFEST", str(tmp_path / "none.json"))
    _device("dev-old")
    async with _client(app) as c:
        r = await c.post("/tunnel/connect", json={
            "device_token": "dev-old", "agent_card": {}, "hermes_version": "x"})
    assert r.status_code == 200 and "plugin" not in r.json()


@pytest.mark.asyncio
async def test_ops_endpoints_dark_without_token_env(app, monkeypatch):
    monkeypatch.delenv("HERALD_RELAY_OPS_TOKEN", raising=False)
    async with _client(app) as c:
        assert (await c.get("/relay/plugin/versions")).status_code == 404
        assert (await c.post("/relay/plugin/update-now", json={"all": True})).status_code == 404


@pytest.mark.asyncio
async def test_ops_endpoints_need_the_token(app, monkeypatch):
    monkeypatch.setenv("HERALD_RELAY_OPS_TOKEN", "s3cret")
    async with _client(app) as c:
        r = await c.get("/relay/plugin/versions", headers={"X-Herald-Ops-Token": "nope"})
        assert r.status_code == 401
        r = await c.post("/relay/plugin/update-now", json={"all": True})
        assert r.status_code == 401


@pytest.mark.asyncio
async def test_versions_counts_active_tunnels(app, manifest_file, monkeypatch):
    monkeypatch.setenv("HERALD_RELAY_OPS_TOKEN", "s3cret")
    _device("a", agent_card={}, plugin_version="0.8.0")
    _device("b", agent_card={}, plugin_version="0.8.0")
    _device("c", agent_card={})                                # pre-0.8 tunnel
    _device("phone", fcm_token="x")                            # not a tunnel
    _device("gone", agent_card={}, plugin_version="0.7.9", last_seen=time.time() - 90 * 86400)
    async with _client(app) as c:
        r = await c.get("/relay/plugin/versions", headers={"X-Herald-Ops-Token": "s3cret"})
    j = r.json()
    assert j["versions"]["0.8.0"] >= 2
    assert j["versions"]["unknown (<0.8.0)"] >= 1
    assert "0.7.9" not in j["versions"]
    assert j["latest"] == "0.8.1" and j["rollout_percent"] == 10


@pytest.mark.asyncio
async def test_update_now_reaches_a_connected_tunnel(app, monkeypatch):
    import asyncio

    from routers import tunnel
    monkeypatch.setenv("HERALD_RELAY_OPS_TOKEN", "s3cret")
    q: asyncio.Queue = asyncio.Queue()
    monkeypatch.setitem(tunnel._plugin_queues, "dev-live", q)
    async with _client(app) as c:
        r = await c.post("/relay/plugin/update-now", json={"device_token": "dev-live"},
                         headers={"X-Herald-Ops-Token": "s3cret"})
    assert r.json()["sent"] == 1
    assert q.get_nowait() == {"type": "bridge_update", "force": True}
