"""Background pushes must fire on Hermes's REAL event names.

The original policy matched ``approval_required`` and ``run_complete``. The
Hermes api_server emits ``approval.request`` and ``run.completed`` — so the
push path never fired at all, and a task running with the app swiped away was
completely silent.

This is the path that makes long tasks useful without a WebRTC session: the
consolidator's text arrives as an FCM data push and is spoken on-device.
"""
import pytest

from herald_relay.push_triggers import PushTriggerPolicy


class TestRealHermesEventNames:
    def test_approval_request_fires_high_urgency(self):
        p = PushTriggerPolicy()
        ok, msg, urgency = p.should_push(
            {"event": "approval.request", "data": {"prompt": "Run rm -rf build?"}},
            run_duration_s=5.0,
        )
        assert ok is True
        assert urgency == "high"
        assert "rm -rf build" in msg

    def test_approval_fires_regardless_of_duration(self):
        """A blocked run needs the user NOW, even if it only just started."""
        p = PushTriggerPolicy()
        ok, _, _ = p.should_push(
            {"event": "approval.request", "data": {}}, run_duration_s=0.1
        )
        assert ok is True

    def test_run_completed_fires_for_a_long_run(self):
        p = PushTriggerPolicy()
        ok, msg, urgency = p.should_push(
            {"event": "run.completed", "data": {"summary": "Deployed to prod"}},
            run_duration_s=30.0,
        )
        assert ok is True
        assert urgency == "low"
        assert "Deployed to prod" in msg

    def test_run_failed_is_reported(self):
        p = PushTriggerPolicy()
        ok, _, _ = p.should_push(
            {"event": "run.failed", "data": {"summary": "Build broke"}},
            run_duration_s=30.0,
        )
        assert ok is True

    def test_short_run_does_not_push(self):
        """Instant tasks shouldn't buzz the phone."""
        p = PushTriggerPolicy()
        ok, _, _ = p.should_push(
            {"event": "run.completed", "data": {}}, run_duration_s=2.0
        )
        assert ok is False

    def test_legacy_names_still_work(self):
        p = PushTriggerPolicy()
        assert p.should_push(
            {"type": "approval_required", "data": {}}, run_duration_s=1.0
        )[0] is True


class TestProgressRateLimiting:
    def test_first_progress_pushes(self):
        p = PushTriggerPolicy(progress_interval_s=60.0)
        ok, msg, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "terminal"}},
            run_duration_s=30.0, run_id="r1",
        )
        assert ok is True
        assert "terminal" in msg

    def test_second_progress_is_suppressed(self):
        """A chatty run must not narrate every tool call into a pocket."""
        p = PushTriggerPolicy(progress_interval_s=60.0)
        p.should_push(
            {"event": "tool.completed", "data": {"name": "terminal"}},
            run_duration_s=30.0, run_id="r1",
        )
        ok, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "read_file"}},
            run_duration_s=31.0, run_id="r1",
        )
        assert ok is False

    def test_progress_not_pushed_for_short_runs(self):
        p = PushTriggerPolicy()
        ok, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "terminal"}},
            run_duration_s=2.0, run_id="r1",
        )
        assert ok is False

    def test_separate_runs_have_separate_budgets(self):
        p = PushTriggerPolicy(progress_interval_s=60.0)
        a, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "t"}},
            run_duration_s=30.0, run_id="r1",
        )
        b, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "t"}},
            run_duration_s=30.0, run_id="r2",
        )
        assert a is True and b is True

    def test_completion_resets_the_progress_budget(self):
        p = PushTriggerPolicy(progress_interval_s=60.0)
        p.should_push(
            {"event": "tool.completed", "data": {"name": "t"}},
            run_duration_s=30.0, run_id="r1",
        )
        p.should_push({"event": "run.completed", "data": {}},
                      run_duration_s=40.0, run_id="r1")
        ok, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "t"}},
            run_duration_s=30.0, run_id="r1",
        )
        assert ok is True

    def test_progress_can_be_disabled(self):
        p = PushTriggerPolicy(push_progress=False)
        ok, _, _ = p.should_push(
            {"event": "tool.completed", "data": {"name": "t"}},
            run_duration_s=99.0, run_id="r1",
        )
        assert ok is False
