"""Tests for EventClassifier — isolated, no server needed."""
import pytest
from herald_relay.event_classifier import (
    EventClassifier,
    IGNORE, ACCUMULATE, MILESTONE, QUESTION, DONE,
)


def clf():
    return EventClassifier()


def test_partial_is_ignored():
    r = clf().feed({"type": "partial", "data": {"text": "hello"}})
    assert r.signal == IGNORE


def test_tool_start_is_ignored():
    r = clf().feed({"type": "tool_start", "data": {"name": "terminal"}})
    assert r.signal == IGNORE


def test_tool_call_is_ignored():
    r = clf().feed({"type": "tool_call"})
    assert r.signal == IGNORE


def test_heartbeat_is_ignored():
    r = clf().feed({"type": "heartbeat"})
    assert r.signal == IGNORE


def test_tool_end_is_accumulate():
    r = clf().feed({"type": "tool_end", "data": {"name": "terminal"}})
    assert r.signal == ACCUMULATE


def test_run_state_is_accumulate():
    r = clf().feed({"type": "run_state", "data": {"mode": "working"}})
    assert r.signal == ACCUMULATE


def test_checkpoint_is_accumulate():
    r = clf().feed({"type": "checkpoint", "data": {"text": "Still working…"}})
    assert r.signal == ACCUMULATE


def test_approval_required_is_question():
    r = clf().feed({"type": "approval_required", "data": {"prompt": "Delete prod?"}})
    assert r.signal == QUESTION
    assert r.spoken_text is not None and "Delete prod?" in r.spoken_text
    assert r.summary is not None and "Delete prod?" in r.summary


def test_approval_request_variant_is_question():
    r = clf().feed({"type": "approval.request", "data": {"prompt": "Sure?"}})
    assert r.signal == QUESTION


def test_question_event_is_question():
    r = clf().feed({"type": "question", "data": {"prompt": "Which file?"}})
    assert r.signal == QUESTION


def test_final_is_done_with_explicit_summary():
    r = clf().feed({"type": "final", "data": {"summary": "All done."}})
    assert r.signal == DONE
    assert r.summary == "All done."


def test_run_complete_is_done():
    r = clf().feed({"type": "run_complete", "data": {"summary": "Built ok."}})
    assert r.signal == DONE
    assert r.summary is not None and "Built ok." in r.summary


def test_done_event_is_done():
    r = clf().feed({"type": "done"})
    assert r.signal == DONE


def test_error_is_done_with_message():
    r = clf().feed({"type": "error", "data": {"message": "OOM"}})
    assert r.signal == DONE
    assert r.summary is not None and "OOM" in r.summary


def test_run_error_is_done():
    r = clf().feed({"type": "run_error", "data": {"message": "timeout"}})
    assert r.signal == DONE


def test_fallback_summary_from_accumulated_text():
    c = clf()
    c.feed({"type": "text", "data": {"text": "Refactored auth module."}})
    r = c.feed({"type": "final", "data": {}})
    assert r.signal == DONE
    assert r.summary is not None and "Refactored" in r.summary


def test_fallback_summary_from_tool_names():
    c = clf()
    c.feed({"type": "tool_end", "data": {"name": "terminal"}})
    c.feed({"type": "tool_end", "data": {"name": "read_file"}})
    r = c.feed({"type": "final", "data": {}})
    assert r.signal == DONE
    assert r.summary  # something was built


def test_empty_event_is_done_with_default_summary():
    r = clf().feed({"type": "final", "data": {}})
    assert r.signal == DONE
    assert r.summary == "Done."


def test_unknown_type_is_accumulate():
    r = clf().feed({"type": "some_future_event"})
    assert r.signal == ACCUMULATE


def test_truncate_long_summary():
    long_text = "x" * 400
    r = clf().feed({"type": "final", "data": {"summary": long_text}})
    assert r.signal == DONE
    assert len(r.summary) <= 301  # 300 chars + ellipsis


def test_question_default_prompt():
    r = clf().feed({"type": "approval_required", "data": {}})
    assert r.signal == QUESTION
    assert r.spoken_text  # not empty
