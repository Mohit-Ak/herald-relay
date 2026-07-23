import pytest, asyncio, json
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

pytest_plugins = ["pytest_asyncio"]

# ---- RelayManager unit tests ----

@pytest.mark.asyncio
async def test_relay_manager_register_and_connect():
    from services.relay_manager import RelayManager
    rm = RelayManager()
    mock_ws = AsyncMock()
    relay_id = await rm.register("tok1", mock_ws)
    assert relay_id
    assert rm.is_connected("tok1")

@pytest.mark.asyncio
async def test_relay_manager_unregister():
    from services.relay_manager import RelayManager
    rm = RelayManager()
    mock_ws = AsyncMock()
    await rm.register("tok2", mock_ws)
    assert rm.is_connected("tok2")
    await rm.unregister("tok2")
    assert not rm.is_connected("tok2")

@pytest.mark.asyncio
async def test_forward_request_device_offline():
    from services.relay_manager import RelayManager
    rm = RelayManager()
    with pytest.raises(ValueError, match="not connected"):
        async for _ in rm.forward_request("ghost", "GET", "/health", None, {}):
            pass

@pytest.mark.asyncio
async def test_relay_manager_connected_count():
    from services.relay_manager import RelayManager
    rm = RelayManager()
    await rm.register("a", AsyncMock())
    await rm.register("b", AsyncMock())
    assert rm.connected_count() == 2
    await rm.unregister("a")
    assert rm.connected_count() == 1

# ---- API endpoint tests ----

@pytest.fixture
def app():
    from main import app
    return app

@pytest.mark.asyncio
async def test_push_register_endpoint(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.post("/push/register", json={
            "fcm_token": "fcm_abc123",
            "platform": "android",
            "plan": "byok",
        })
    assert resp.status_code == 200
    data = resp.json()
    assert "device_token" in data
    assert "relay_url" in data

@pytest.mark.asyncio
async def test_billing_credits_default(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # Register first
        reg = await client.post("/push/register", json={"fcm_token": "x", "platform": "android", "plan": "byok"})
        token = reg.json()["device_token"]
        resp = await client.get(f"/billing/credits?device_token={token}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["free_bursts_remaining"] == 20

@pytest.mark.asyncio
async def test_billing_add_credits(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        reg = await client.post("/push/register", json={"fcm_token": "x", "platform": "android", "plan": "credits"})
        token = reg.json()["device_token"]
        resp = await client.post("/billing/credits/add", json={"device_token": token, "amount_usd": 5.0})
    assert resp.status_code == 200
    assert resp.json()["credits_added"] == 100

@pytest.mark.asyncio
async def test_health_endpoint(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert "connected_devices" in resp.json()
