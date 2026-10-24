"""#263: a dead FCM token for a device whose plugin is CONNECTED must alert.

Production: the plugin on one machine streamed as ``3865555c…`` while the phone
registered its FCM token only under another machine's ``52b6f5f8…``. The next
push for ``3865555c…`` got 404 UNREGISTERED, ``_send_fcm`` cleared the token at
WARNING ("device must re-register") and every later push skipped at WARNING
too. A live agent's pushes were being dropped and nothing alerted.

When the plugin is connected the clear is an outage, so it logs at ERROR; a
stale token for an offline device stays routine housekeeping (WARNING).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DEV = "3865555c-0000-4000-8000-000000000000"


def _run(monkeypatch, *, fcm_token, fcm_status=404, connected):
    import routers.tunnel as tunnel

    store = {DEV: {"fcm_token": fcm_token}}

    class _Ref:
        def get(self):
            class _D:
                exists = True

                @staticmethod
                def to_dict():
                    return dict(store[DEV])
            return _D()

        def update(self, data):
            store[DEV].update(data)

    class _Col:
        def document(self, _k):
            return _Ref()

    class _DB:
        def collection(self, _n):
            return _Col()

    monkeypatch.setattr(tunnel, "get_db", lambda: _DB())
    monkeypatch.setenv("FCM_PROJECT_ID", "proj")
    monkeypatch.setattr(tunnel, "is_plugin_connected", lambda t: connected)

    import google.auth
    import google.auth.transport.requests

    class _Creds:
        token = "tok"

        def refresh(self, _r):
            return None

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_Creds(), None))
    monkeypatch.setattr(google.auth.transport.requests, "Request", lambda: object())

    class _Resp:
        status_code = fcm_status
        text = '{"error":{"details":[{"errorCode":"UNREGISTERED"}]}}' if fcm_status == 404 else "{}"

        def raise_for_status(self):
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    # A private loop: asyncio.run() would close the default loop that other
    # tests in this suite reuse via get_event_loop().
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(
            tunnel._send_fcm(DEV, title="t", body="b", data={"signal": "DONE"})
        )
    finally:
        loop.close()
    return store[DEV]


def _levels(caplog):
    return [r.levelno for r in caplog.records if "[FCM]" in r.getMessage()]


def test_unregistered_while_plugin_connected_is_an_error(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="routers.tunnel")
    doc = _run(monkeypatch, fcm_token="f" * 152, connected=True)
    assert doc["fcm_token"] is None, "dead token must still be cleared"
    assert logging.ERROR in _levels(caplog)
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "PUSH ROUTING BROKEN" in msg
    assert DEV not in msg, "only the 8-char prefix may be logged"


def test_unregistered_for_offline_device_stays_a_warning(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="routers.tunnel")
    doc = _run(monkeypatch, fcm_token="f" * 152, connected=False)
    assert doc["fcm_token"] is None
    assert _levels(caplog) == [logging.WARNING]


@pytest.mark.parametrize("connected,level", [(True, logging.ERROR), (False, logging.WARNING)])
def test_missing_token_alerts_only_when_plugin_connected(monkeypatch, caplog, connected, level):
    caplog.set_level(logging.WARNING, logger="routers.tunnel")
    _run(monkeypatch, fcm_token=None, connected=connected)
    assert _levels(caplog) == [level]
