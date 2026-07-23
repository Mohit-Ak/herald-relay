"""Tests for PushTriggerPolicy."""
import pytest
from herald_relay.push_triggers import PushTriggerPolicy


@pytest.fixture
def default_policy():
    return PushTriggerPolicy(push_on_approval=True, push_on_done=True, min_run_duration_for_push_s=10.0)


def test_approval_required_is_high_urgency(default_policy):
    event = {"type": "approval_required", "data": {"prompt": "Delete production DB?"}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=5.0)

    assert should is True
    assert "Delete production DB?" in message
    assert urgency == "high"


def test_approval_required_default_prompt(default_policy):
    event = {"type": "approval_required", "data": {}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=1.0)

    assert should is True
    assert "Hermes needs your approval" in message
    assert urgency == "high"


def test_run_complete_long_run_triggers_push(default_policy):
    event = {"type": "run_complete", "data": {"summary": "Refactor complete"}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=30.0)

    assert should is True
    assert "Refactor complete" in message
    assert urgency == "low"


def test_final_event_long_run_triggers_push(default_policy):
    event = {"type": "final", "data": {"summary": "All done"}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=15.0)

    assert should is True
    assert "All done" in message
    assert urgency == "low"


def test_run_complete_short_run_no_push(default_policy):
    event = {"type": "run_complete", "data": {"summary": "Fast task"}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=3.0)

    assert should is False
    assert message == ""
    assert urgency == ""


def test_run_complete_exactly_at_threshold(default_policy):
    event = {"type": "run_complete", "data": {"summary": "Borderline"}}
    # Exactly at threshold — should push
    should, _, _ = default_policy.should_push(event, run_duration_s=10.0)
    assert should is True

    # Just below threshold — should not push
    should, _, _ = default_policy.should_push(event, run_duration_s=9.99)
    assert should is False


def test_push_off_no_trigger():
    policy = PushTriggerPolicy(push_on_approval=True, push_on_done=False)
    event = {"type": "run_complete", "data": {"summary": "Done"}}
    should, message, urgency = policy.should_push(event, run_duration_s=60.0)

    assert should is False
    assert message == ""


def test_approval_push_off():
    policy = PushTriggerPolicy(push_on_approval=False, push_on_done=True)
    event = {"type": "approval_required", "data": {"prompt": "Confirm?"}}
    should, message, urgency = policy.should_push(event, run_duration_s=5.0)

    assert should is False


def test_unknown_event_no_push(default_policy):
    event = {"type": "tool_call", "data": {"tool": "bash"}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=100.0)

    assert should is False
    assert message == ""
    assert urgency == ""


def test_run_complete_default_summary(default_policy):
    event = {"type": "run_complete", "data": {}}
    should, message, urgency = default_policy.should_push(event, run_duration_s=20.0)

    assert should is True
    assert "Task complete" in message
