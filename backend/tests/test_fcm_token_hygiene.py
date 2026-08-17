"""FCM token hygiene: a good token must survive, junk must never displace it.

Production had 5 of 7 device records holding unusable `fcm_token` values (7-22
chars: "android", a bare uuid, None). Two causes, both fixed here:

1. ``/push/register`` wrote ``req.fcm_token`` with an unconditional ``set()``.
   The field is optional and the Flutter connect screen posts only
   ``{platform, plan}``, so a routine re-registration wrote ``None`` straight
   over a working token and silently disabled background push for that device.
2. Nothing ever cleared tokens FCM had permanently retired, so dead entries
   accumulated and every push retried them into a 404.
"""
from __future__ import annotations

import os
import sys

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from routers.push import is_valid_fcm_token  # noqa: E402

REAL = "f" * 152          # plausible length for a genuine registration token
JUNK = "android"          # what actually showed up in production


class TestTokenValidator:

    def test_real_token_is_valid(self):
        assert is_valid_fcm_token(REAL) is True

    @pytest.mark.parametrize("bad", [None, "", "   ", JUNK, "x" * 22, "x" * 99])
    def test_placeholders_are_rejected(self, bad):
        assert is_valid_fcm_token(bad) is False


class TestRegisterDoesNotClobber:
    """The exact sequence that wiped tokens in production."""

    def _client(self, monkeypatch, store):
        import routers.push as push

        class _Doc:
            def __init__(self, data):
                self._data = data

            @property
            def exists(self):
                return self._data is not None

            def to_dict(self):
                return dict(self._data or {})

        class _Ref:
            def __init__(self, key):
                self.key = key

            def get(self):
                return _Doc(store.get(self.key))

            def set(self, data):
                store[self.key] = dict(data)

        class _Col:
            def document(self, key):
                return _Ref(key)

        class _DB:
            def collection(self, _n):
                return _Col()

        monkeypatch.setattr(push, "get_db", lambda: _DB())

        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(push.router)
        return TestClient(app)

    def test_reregister_without_token_keeps_the_existing_one(self, monkeypatch):
        """THE bug: the connect screen re-registers with no fcm_token.

        Before the fix this stored None and background push died silently.
        """
        store = {}
        cli = self._client(monkeypatch, store)

        r1 = cli.post("/push/register",
                      json={"platform": "android", "plan": "byok",
                            "fcm_token": REAL})
        assert r1.status_code == 200
        dev = r1.json()["device_token"]
        assert store[dev]["fcm_token"] == REAL

        # Re-register exactly as the app does: no fcm_token at all.
        r2 = cli.post("/push/register",
                      json={"device_token": dev, "platform": "android",
                            "plan": "byok"})
        assert r2.status_code == 200
        assert store[dev]["fcm_token"] == REAL, "a working token was clobbered"

    def test_junk_token_never_displaces_a_real_one(self, monkeypatch):
        store = {}
        cli = self._client(monkeypatch, store)
        r1 = cli.post("/push/register",
                      json={"platform": "android", "plan": "byok",
                            "fcm_token": REAL})
        dev = r1.json()["device_token"]

        cli.post("/push/register",
                 json={"device_token": dev, "platform": "android",
                       "plan": "byok", "fcm_token": JUNK})
        assert store[dev]["fcm_token"] == REAL

    def test_a_real_token_does_upgrade_an_empty_record(self, monkeypatch):
        """Registering before push is enabled, then attaching a token later."""
        store = {}
        cli = self._client(monkeypatch, store)
        r1 = cli.post("/push/register",
                      json={"platform": "android", "plan": "byok"})
        dev = r1.json()["device_token"]
        assert store[dev]["fcm_token"] is None

        cli.post("/push/register",
                 json={"device_token": dev, "platform": "android",
                       "plan": "byok", "fcm_token": REAL})
        assert store[dev]["fcm_token"] == REAL

    def test_credits_and_registered_at_survive_reregistration(self, monkeypatch):
        """Guard the pre-existing merge behaviour while changing this code."""
        store = {}
        cli = self._client(monkeypatch, store)
        dev = cli.post("/push/register",
                       json={"platform": "android", "plan": "byok",
                             "fcm_token": REAL}).json()["device_token"]
        store[dev]["credits"] = 7
        first_seen = store[dev]["registered_at"]

        cli.post("/push/register",
                 json={"device_token": dev, "platform": "android", "plan": "byok"})
        assert store[dev]["credits"] == 7
        assert store[dev]["registered_at"] == first_seen
