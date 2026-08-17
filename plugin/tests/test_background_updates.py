"""Regression tests for the app-is-closed update path.

Every test here pins a bug that was live in production. The common thread is
that all five failed **silently**: the code ran, returned success, and the user
simply never heard anything.

1. ``message.delta`` was not in the ignore set, so it fell through to the
   "unknown type" branch and was ACCUMULATE — one relay POST per token.
2. ``PushTriggerPolicy`` had zero production callers, so mid-run progress was
   never pushed at all.
3. A stream that ended or stalled without a terminal event exited the loop
   silently; the user waited forever for a task that was already dead.
4. The 300 s approval timeout auto-denied any question asked while the user
   was away.
5. The FCM data payload omitted ``type``/``message``/``urgency``, which the
   Flutter background isolate requires — so nothing was ever spoken.
"""
from __future__ import annotations

import asyncio

import pytest

from herald_relay.event_classifier import (
    EventClassifier, ACCUMULATE, DONE, IGNORE, MILESTONE, QUESTION,
)
from herald_relay.push_triggers import PushTriggerPolicy


# ---------------------------------------------------------------------------
# 1. The token firehose must be silent
# ---------------------------------------------------------------------------

class TestRealHermesVocabulary:
    """Hermes emits dotted, past-tense names — see api_server.py."""

    def test_message_delta_is_ignored(self):
        """The single highest-volume event must never reach the user.

        This is the "updating every small detail" bug: message.delta fired
        hundreds of times per run and each one became a /tunnel/update POST.
        """
        c = EventClassifier()
        for _ in range(50):
            ev = c.feed({"type": "message.delta", "data": {"delta": "hi "}})
            assert ev.signal == IGNORE

    def test_tool_started_is_ignored_but_completed_accumulates(self):
        """Starts are shown visually; narrating them double-talks."""
        c = EventClassifier()
        assert c.feed(
            {"type": "tool.started", "data": {"name": "terminal"}}
        ).signal == IGNORE
        assert c.feed(
            {"type": "tool.completed", "data": {"name": "terminal"}}
        ).signal == ACCUMULATE

    def test_run_started_is_ignored(self):
        c = EventClassifier()
        assert c.feed({"type": "run.started", "data": {}}).signal == IGNORE

    def test_tool_progress_is_a_milestone(self):
        """tool.progress is the real long-task heartbeat from Hermes."""
        c = EventClassifier()
        out = c.feed({"type": "tool.progress", "data": {"text": "step 3/9"}})
        assert out.signal == MILESTONE

    def test_run_failed_is_terminal_and_spoken(self):
        c = EventClassifier()
        out = c.feed({"type": "run.failed", "data": {"message": "boom"}})
        assert out.signal == DONE
        assert "boom" in (out.spoken_text or "")

    def test_approval_request_is_a_question(self):
        c = EventClassifier()
        out = c.feed(
            {"type": "approval.request", "data": {"prompt": "Delete /tmp?"}}
        )
        assert out.signal == QUESTION
        assert out.spoken_text == "Delete /tmp?"

    def test_summary_is_built_from_delta_text(self):
        """The answer streams under `delta`, not `text`.

        Reading only `text` meant the final spoken summary fell back to
        "Done — ran terminal." instead of the actual answer.
        """
        c = EventClassifier()
        for chunk in ("The answer ", "is ", "42."):
            c.feed({"type": "message.delta", "data": {"delta": chunk}})
        out = c.feed({"type": "run.completed", "data": {}})
        assert out.signal == DONE
        assert "42" in (out.summary or "")

    def test_approval_prompt_does_not_pollute_the_summary(self):
        """An approval question is not the assistant's answer."""
        c = EventClassifier()
        c.feed({"type": "approval.request",
                "data": {"prompt": "Should I delete everything?"}})
        c.feed({"type": "message.delta", "data": {"delta": "All clean."}})
        out = c.feed({"type": "run.completed", "data": {}})
        assert "delete everything" not in (out.summary or "").lower()
        assert "All clean." in (out.summary or "")


# ---------------------------------------------------------------------------
# 2. Long runs must stay audible, without becoming chatty
# ---------------------------------------------------------------------------

class TestLongRunAudibility:

    def test_progress_is_rate_limited_not_silent(self):
        """An hour-long task should speak periodically — but not per tool."""
        p = PushTriggerPolicy(progress_interval_s=60.0,
                              min_run_duration_for_push_s=10.0)
        ev = {"type": "tool.completed", "data": {"name": "terminal"}}

        ok, _, urgency = p.should_push(ev, run_duration_s=30.0, run_id="r1")
        assert ok is True and urgency == "low"

        # Immediately after, the same event must stay quiet.
        ok2, _, _ = p.should_push(ev, run_duration_s=31.0, run_id="r1")
        assert ok2 is False

    def test_short_runs_never_push_progress(self):
        """A 3-second task shouldn't wake the phone mid-flight."""
        p = PushTriggerPolicy(min_run_duration_for_push_s=10.0)
        ok, _, _ = p.should_push(
            {"type": "tool.completed", "data": {"name": "x"}},
            run_duration_s=3.0, run_id="r2",
        )
        assert ok is False

    def test_approval_bypasses_all_rate_limits(self):
        """A blocked run is the one thing that must ALWAYS interrupt."""
        p = PushTriggerPolicy()
        for _ in range(5):
            ok, msg, urgency = p.should_push(
                {"type": "approval.request", "data": {"prompt": "yes?"}},
                run_duration_s=0.1, run_id="r3",
            )
            assert ok is True
            assert urgency == "high"
            assert msg == "yes?"

    def test_tool_progress_is_a_push_candidate(self):
        p = PushTriggerPolicy(min_run_duration_for_push_s=10.0)
        ok, _, _ = p.should_push(
            {"type": "tool.progress", "data": {"text": "halfway"}},
            run_duration_s=120.0, run_id="r4",
        )
        assert ok is True

    def test_forget_releases_run_state(self):
        """Per-run state must not leak on a long-lived daemon."""
        p = PushTriggerPolicy()
        p.should_push({"type": "tool.completed", "data": {"name": "t"}},
                      run_duration_s=99.0, run_id="r5")
        assert "r5" in p._last_progress_at
        p.forget("r5")
        assert "r5" not in p._last_progress_at


# ---------------------------------------------------------------------------
# 3. The watchdog: never fail silently
# ---------------------------------------------------------------------------

class _FakeClient:
    """Captures the updates the plugin would POST to the relay."""

    def __init__(self):
        self.updates: list[dict] = []


def _make_client(events, *, stall=False):
    """Build a HeraldRelayClient whose Hermes stream yields `events`."""
    from herald_relay import relay_client as rc

    c = rc.HeraldRelayClient(
        relay_url="http://relay.test",
        device_token="dev",
        local_hermes_url="http://hermes.test",
        hermes_key="k",
    )
    captured: list[dict] = []

    async def _post_update(_client, update):
        captured.append(update)

    async def _hermes_post(*_a, **_k):
        return {"run_id": "run1"}

    async def _ensure_session():
        return "sess1"

    async def _sse(_url):
        for e in events:
            yield e
        if stall:
            await asyncio.sleep(3600)  # never yields again

    c._post_update = _post_update           # type: ignore[assignment]
    c._hermes_post = _hermes_post           # type: ignore[assignment]
    c._ensure_session = _ensure_session     # type: ignore[assignment]
    c._hermes_sse = _sse                    # type: ignore[assignment]
    return c, captured


@pytest.mark.asyncio
async def test_stream_ending_without_terminal_event_still_notifies():
    """Hermes dying mid-run must reach the user.

    Before: the `async for` simply ended and the coroutine returned. Cloud and
    the user both waited forever for a run that was already gone.
    """
    c, captured = _make_client([
        {"type": "run.started", "data": {}},
        {"type": "tool.completed", "data": {"name": "terminal"}},
        # ...and then nothing. No run.completed.
    ])
    await c._execute_and_stream(_FakeClient(), "task1", "run1", "do it")

    terminal = [u for u in captured if u["signal"] == DONE]
    assert terminal, "a dead stream must still produce a terminal update"
    assert "unexpectedly" in terminal[-1]["summary"]
    assert terminal[-1].get("spoken_text"), "the user must be TOLD, out loud"


@pytest.mark.asyncio
async def test_stalled_stream_triggers_the_watchdog(monkeypatch):
    """A hung run must be reported, not waited on forever."""
    from herald_relay import relay_client as rc
    monkeypatch.setattr(rc, "_STALL_TIMEOUT_S", 0.2)

    c, captured = _make_client(
        [{"type": "tool.completed", "data": {"name": "terminal"}}],
        stall=True,
    )
    await asyncio.wait_for(
        c._execute_and_stream(_FakeClient(), "task1", "run1", "do it"),
        timeout=10,
    )

    terminal = [u for u in captured if u["signal"] == DONE]
    assert terminal, "a stalled stream must produce a terminal update"
    assert "stuck" in terminal[-1]["spoken_text"].lower()


@pytest.mark.asyncio
async def test_ignore_events_are_never_posted_to_the_relay():
    """The bandwidth/chatter fix, asserted end to end."""
    c, captured = _make_client([
        {"type": "message.delta", "data": {"delta": "a"}},
        {"type": "message.delta", "data": {"delta": "b"}},
        {"type": "message.delta", "data": {"delta": "c"}},
        {"type": "run.completed", "data": {}},
    ])
    await c._execute_and_stream(_FakeClient(), "task1", "run1", "hi")

    assert all(u["signal"] != IGNORE for u in captured)
    # 3 deltas collapse to nothing; only the terminal update survives.
    assert len(captured) == 1
    assert captured[0]["signal"] == DONE


@pytest.mark.asyncio
async def test_approval_timeout_is_spoken_not_silent(monkeypatch):
    """Timing out on a question must say so rather than dying quietly."""
    from herald_relay import relay_client as rc
    monkeypatch.setattr(rc, "_APPROVAL_TIMEOUT_S", 0.1)

    c, captured = _make_client([
        {"type": "approval.request", "data": {"prompt": "Delete it?"}},
    ])
    await asyncio.wait_for(
        c._execute_and_stream(_FakeClient(), "task1", "run1", "go"),
        timeout=10,
    )

    terminal = [u for u in captured if u["signal"] == DONE]
    assert terminal
    assert "approval" in terminal[-1]["summary"].lower()
    assert terminal[-1].get("spoken_text")


def test_approval_window_is_generous_by_default():
    """300 s auto-denied anything asked while the user was asleep."""
    from herald_relay import relay_client as rc
    assert rc._APPROVAL_TIMEOUT_S >= 1800
