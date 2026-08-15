"""Device registration must accept what the Flutter app actually sends.

Two live bugs, both surfacing to the user as an opaque failure on the
Connect screen:

1. ``fcm_token`` was REQUIRED -> every pairing attempt 422'd, because
   ``relay_connect_screen.dart`` posts only ``{platform, plan}``.
2. The returned ``relay_url`` was built from ``HERALD_RELAY_URL``, which on
   the box was ``http://localhost:8082`` -> the phone was told to dial
   ITSELF. The packaged default (``wss://relay.herald.app``) is an
   unregistered domain, so the fallback was equally dead.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """App exposing only the push router. Firestore is faked by the
    autouse ``mock_firestore`` fixture in conftest.py."""
    import routers.push as push

    app = FastAPI()
    app.include_router(push.router)
    return TestClient(app)


class TestRegisterAcceptsAppPayload:
    def test_app_payload_without_fcm_token_succeeds(self, client):
        """EXACTLY what relay_connect_screen.dart sends. Was a 422."""
        r = client.post(
            "/push/register", json={"platform": "android", "plan": "self_hosted"}
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["device_token"]
        assert body["relay_url"].endswith("/relay/connect")

    def test_cloud_plan_also_accepted(self, client):
        r = client.post("/push/register", json={"platform": "android", "plan": "cloud"})
        assert r.status_code == 200, r.text

    def test_fcm_token_still_stored_when_supplied(self, client):
        r = client.post(
            "/push/register",
            json={"platform": "android", "plan": "cloud", "fcm_token": "tok-123"},
        )
        assert r.status_code == 200, r.text


class TestRelayUrlIsDialableByTheDevice:
    def test_localhost_config_is_not_handed_to_the_device(self, client, monkeypatch):
        """A phone told to dial localhost would connect to itself."""
        monkeypatch.setenv("HERALD_RELAY_URL", "http://localhost:8082")
        r = client.post(
            "/push/register",
            json={"platform": "android", "plan": "self_hosted"},
            headers={"Host": "34.173.138.246:8082"},
        )
        assert r.status_code == 200, r.text
        url = r.json()["relay_url"]
        assert "localhost" not in url
        assert url == "ws://34.173.138.246:8082/relay/connect"

    def test_placeholder_domain_is_not_handed_to_the_device(self, client, monkeypatch):
        monkeypatch.setenv("HERALD_RELAY_URL", "wss://relay.herald.app")
        r = client.post(
            "/push/register",
            json={"platform": "android", "plan": "cloud"},
            headers={"Host": "34.173.138.246:8082"},
        )
        assert r.status_code == 200, r.text
        assert "relay.herald.app" not in r.json()["relay_url"]

    def test_real_configured_url_wins(self, client, monkeypatch):
        monkeypatch.setenv("HERALD_RELAY_URL", "https://relay.example.com")
        r = client.post(
            "/push/register",
            json={"platform": "android", "plan": "cloud"},
            headers={"Host": "34.173.138.246:8082"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["relay_url"] == "wss://relay.example.com/relay/connect"

    def test_forwarded_headers_are_honoured(self, client, monkeypatch):
        monkeypatch.setenv("HERALD_RELAY_URL", "http://localhost:8082")
        r = client.post(
            "/push/register",
            json={"platform": "android", "plan": "cloud"},
            headers={
                "Host": "internal:8082",
                "X-Forwarded-Host": "relay.public.example",
                "X-Forwarded-Proto": "https",
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["relay_url"] == "wss://relay.public.example/relay/connect"
