"""The FCM data payload must satisfy the Flutter background-isolate contract.

This is the bug that silently killed **all** background audio: the relay sent
``{task_id, run_id, signal}``, but ``_firebaseMessagingBackgroundHandler`` in
``flutter/lib/services/push_service.dart`` only speaks when the data payload
carries::

    type     == 'herald_burst' | 'herald_question'
    message  == the text to speak
    urgency  == 'high' | 'low'

The `type` check never matched, so the handler returned immediately and nothing
was ever spoken while the app was closed. Both sides "worked"; the feature did
not exist.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _capture_payload(monkeypatch, *, title, body, data):
    """Run _send_fcm with the network + Firestore stubbed, return the payload."""
    import routers.tunnel as tunnel

    sent: dict = {}

    class _Doc:
        exists = True

        @staticmethod
        def to_dict():
            # Must be a plausible-length token: _send_fcm now skips tokens too
            # short to be real (see is_valid_fcm_token / token hygiene tests).
            return {"fcm_token": "f" * 152}

    class _Col:
        def document(self, _t):
            return self

        def get(self):
            return _Doc()

    class _DB:
        def collection(self, _n):
            return _Col()

    monkeypatch.setattr(tunnel, "get_db", lambda: _DB())
    monkeypatch.setenv("FCM_PROJECT_ID", "proj")

    class _Creds:
        token = "tok"

        def refresh(self, _r):
            return None

    import google.auth
    import google.auth.transport.requests

    monkeypatch.setattr(google.auth, "default", lambda scopes=None: (_Creds(), None))
    monkeypatch.setattr(
        google.auth.transport.requests, "Request", lambda: object()
    )

    class _Resp:
        @staticmethod
        def raise_for_status():
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, _url, headers=None, json=None, timeout=None):
            sent.update(json)
            return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())

    import asyncio

    asyncio.get_event_loop().run_until_complete(
        tunnel._send_fcm("devtok", title=title, body=body, data=data)
    )
    return sent["message"]["data"]


class TestFcmSpeechContract:

    def test_done_push_is_speakable(self, monkeypatch):
        data = _capture_payload(
            monkeypatch,
            title="Task complete",
            body="Deployed the backend.",
            data={"task_id": "t", "run_id": "r", "signal": "DONE"},
        )
        assert data["type"] == "herald_burst"
        assert data["message"] == "Deployed the backend."
        assert data["urgency"] == "low"

    def test_question_push_is_high_urgency(self, monkeypatch):
        """An approval must preempt in-flight speech, so it must be 'high'."""
        data = _capture_payload(
            monkeypatch,
            title="Hermes needs your input",
            body="Delete the bucket?",
            data={"task_id": "t", "run_id": "r", "signal": "QUESTION"},
        )
        assert data["type"] == "herald_question"
        assert data["urgency"] == "high"
        assert data["message"] == "Delete the bucket?"

    def test_milestone_push_is_speakable(self, monkeypatch):
        """Long-run progress is what keeps an hour-long task from feeling dead."""
        data = _capture_payload(
            monkeypatch,
            title="Still working",
            body="Still going — finished terminal.",
            data={"task_id": "t", "run_id": "r", "signal": "MILESTONE"},
        )
        assert data["type"] == "herald_burst"
        assert data["message"] == "Still going — finished terminal."
