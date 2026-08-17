"""
Herald Relay — event classifier.

Maps raw Hermes SSE events to the 5-tier signal used by the tunnel protocol:

  IGNORE      – noise / scaffolding (tool_start, partial text, etc.)
  ACCUMULATE  – informational; buffer for summary, don't push
  MILESTONE   – noteworthy progress; update Firestore task status
  QUESTION    – requires user attention (approval, question); FCM high-priority
  DONE        – run completed or errored; FCM low-priority with summary
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal constants
# ---------------------------------------------------------------------------
IGNORE = "IGNORE"
ACCUMULATE = "ACCUMULATE"
MILESTONE = "MILESTONE"
QUESTION = "QUESTION"
DONE = "DONE"


@dataclass
class ClassifiedEvent:
    signal: str                    # one of the 5 constants above
    summary: Optional[str] = None  # human-readable summary for MILESTONE/DONE
    spoken_text: Optional[str] = None  # what Herald should say for QUESTION/DONE


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class EventClassifier:
    """Stateful classifier that maps Hermes SSE events to Herald signals.

    Keeps a rolling buffer of partial text so it can build a meaningful
    summary for DONE / MILESTONE without an extra LLM call.
    """

    # Events that are pure noise.
    #
    # ``message.delta`` is the token-by-token stream — it is BY FAR the highest
    # volume event Hermes emits (hundreds per run). It was not listed here, so
    # it fell through to the "unknown type" branch and was classified
    # ACCUMULATE, meaning every single token chunk was POSTed to the relay as
    # its own /tunnel/update. That is the "updating every small detail" flood.
    # Its text is still harvested below for the DONE summary; it is just never
    # a reason to talk to the user.
    _IGNORE_TYPES = {
        "message.delta", "message.started", "reasoning.available",
        "run.started", "run.stopping", "approval.responded",
        "partial",          # streaming tokens — too noisy
        "tool.started",     # starts are visual-only; narrating them double-talks
        "tool_start", "tool.start", "tool_call",
        "heartbeat", "ping", "comment",
    }

    # Events that are informational but don't warrant waking the user.
    _ACCUMULATE_TYPES = {
        "tool.completed", "tool.failed", "message.completed",
        "tool_end", "tool.end", "tool_result", "tool_finished",
        "checkpoint", "run_state",
    }

    # Events that are noteworthy progress milestones.
    _MILESTONE_TYPES = {
        "tool.progress",
        "milestone",
        "progress",
    }

    # Terminal events
    _DONE_TYPES = {
        "run.completed", "run.cancelled",
        "final", "run_complete", "done", "complete",
    }

    # Error terminal events
    _ERROR_TYPES = {
        "run.failed",
        "error",
        "run_error",
    }

    # Question / approval events
    _QUESTION_TYPES = {
        "approval.request",
        "approval_required",
        "approval",
        "question",
    }

    # Events whose text must NOT be folded into the answer summary. Scaffolding
    # and approval prompts are not the assistant's answer; harvesting them makes
    # the final spoken summary quote the approval question back at the user.
    _NEVER_HARVEST = {
        "heartbeat", "ping", "comment",
        "tool.started", "tool_start", "tool.start", "tool_call",
        "run.started", "run.stopping",
        "approval.request", "approval_required", "approval", "question",
        "approval.responded",
    }

    def __init__(self) -> None:
        self._text_parts: list[str] = []
        self._tool_names: list[str] = []

    def feed(self, event: dict) -> ClassifiedEvent:
        """Classify a single Hermes SSE event.

        Args:
            event: Parsed Hermes SSE event dict with at least a ``type`` key.

        Returns:
            ClassifiedEvent with signal + optional summary/spoken_text.
        """
        etype = event.get("type", "")
        data = event.get("data", {})
        if not isinstance(data, dict):
            data = {}

        # Accumulate assistant text for the DONE summary.
        #
        # Hermes streams the answer as ``message.delta`` with the chunk under
        # **``delta``**, not ``text``. That event is IGNORE (too noisy to speak),
        # but its text is exactly what we need for the final spoken summary, so
        # harvest it BEFORE the ignore-routing below. Reading only ``text`` and
        # only for non-ignored events meant the summary was always built from
        # the tool-name fallback ("Done — ran terminal.") instead of the actual
        # answer.
        text_chunk = (
            data.get("delta")
            or data.get("text")
            or data.get("content")
            or data.get("message")
            or ""
        )
        if text_chunk and etype not in self._NEVER_HARVEST:
            self._text_parts.append(str(text_chunk))

        # Track tool names for milestone summary
        tool_name = data.get("name") or data.get("tool") or ""
        if tool_name and etype in self._ACCUMULATE_TYPES:
            self._tool_names.append(str(tool_name))

        # ── Routing ─────────────────────────────────────────────────────
        if etype in self._IGNORE_TYPES:
            return ClassifiedEvent(signal=IGNORE)

        if etype in self._QUESTION_TYPES:
            prompt = data.get("prompt") or data.get("question") or "Hermes needs your input"
            return ClassifiedEvent(
                signal=QUESTION,
                spoken_text=prompt,
                summary=prompt,
            )

        if etype in self._DONE_TYPES:
            summary = self._build_summary(data)
            spoken = _truncate(summary, 200)
            return ClassifiedEvent(
                signal=DONE,
                summary=summary,
                spoken_text=spoken,
            )

        if etype in self._ERROR_TYPES:
            msg = data.get("message") or data.get("error") or "Task failed"
            return ClassifiedEvent(
                signal=DONE,
                summary=f"Error: {msg}",
                spoken_text=f"Task failed: {_truncate(msg, 150)}",
            )

        if etype in self._MILESTONE_TYPES:
            text = data.get("text") or data.get("summary") or self._rollup_pending()
            return ClassifiedEvent(
                signal=MILESTONE,
                summary=text,
            )

        if etype in self._ACCUMULATE_TYPES:
            return ClassifiedEvent(
                signal=ACCUMULATE,
                summary=self._rollup_pending() if self._tool_names else None,
            )

        # Unknown event type — treat as ACCUMULATE to be safe
        logger.debug("Unknown event type %r — treating as ACCUMULATE", etype)
        return ClassifiedEvent(signal=ACCUMULATE)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_summary(self, data: dict) -> str:
        """Build a human-readable summary for terminal events."""
        # Prefer explicit summary field
        explicit = data.get("summary") or data.get("text") or data.get("message")
        if explicit:
            return _truncate(str(explicit), 300)

        # Fall back to accumulated text
        if self._text_parts:
            combined = " ".join(self._text_parts).strip()
            return _truncate(combined, 300) if combined else "Done."

        if self._tool_names:
            tools = ", ".join(self._tool_names[-3:])
            return f"Done — ran {tools}."

        return "Done."

    def _rollup_pending(self) -> str:
        """One-liner from accumulated tool names."""
        if not self._tool_names:
            return "Still working…"
        recent = self._tool_names[-3:]
        return "Ran " + ", ".join(recent) + "."


def _truncate(text: str, max_chars: int) -> str:
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= max_chars:
        return text
    return text[:max_chars - 1] + "…"
