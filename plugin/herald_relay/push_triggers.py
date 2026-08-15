"""Push trigger policy — decides when Hermes should wake the user via push.

This is the path that keeps a long task useful **after the app is swiped away**.
No WebRTC session exists then, so updates travel as FCM data pushes and are
spoken on-device with local TTS (see PushService / SpokenUpdate in the Flutter
client). That costs no tokens and no media plane — the text is already produced
by the consolidator.

Event names come from the Hermes api_server (gateway/platforms/api_server.py):
``run.completed`` / ``run.failed`` / ``approval.request`` / ``tool.*``. The
original implementation matched ``approval_required`` / ``run_complete``, which
Hermes NEVER emits, so background pushes silently never fired.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


class PushTriggerPolicy:
    """Decides when Hermes should wake the user via push notification."""

    # High urgency (immediate, with sound): the run is BLOCKED on the user.
    HIGH_URGENCY_EVENTS = {
        "approval.request", "approval_required", "approval",
    }

    # Low urgency (banner): terminal states worth knowing about.
    LOW_URGENCY_EVENTS = {
        "run.completed", "run.failed", "run.cancelled",
        "run_complete", "run_error", "final",
    }

    # Mid-run progress. Only spoken when the run is long AND the interval has
    # elapsed — otherwise a background task would narrate every tool call to a
    # user who isn't even looking at the phone.
    PROGRESS_EVENTS = {
        "tool.completed", "tool_end", "tool.end", "checkpoint",
    }

    def __init__(
        self,
        push_on_approval: bool = True,
        push_on_done: bool = True,
        push_progress: bool = True,
        min_run_duration_for_push_s: float = 10.0,
        progress_interval_s: float = 60.0,
    ):
        """
        Args:
            push_on_approval: High-urgency push when Hermes needs approval.
            push_on_done: Push when a run reaches a terminal state.
            push_progress: Occasional spoken progress while the app is closed.
            min_run_duration_for_push_s: Only push on completion if the run took
                at least this long (avoids spamming for instant tasks).
            progress_interval_s: Minimum gap between background progress pushes.
        """
        self.push_on_approval = push_on_approval
        self.push_on_done = push_on_done
        self.push_progress = push_progress
        self.min_run_duration_for_push_s = min_run_duration_for_push_s
        self.progress_interval_s = progress_interval_s
        self._last_progress_at: dict[str, float] = {}

    @staticmethod
    def _event_type(event: dict) -> str:
        return str(event.get("type") or event.get("event") or "")

    @staticmethod
    def _data(event: dict) -> dict:
        d = event.get("data")
        return d if isinstance(d, dict) else event

    def should_push(
        self, event: dict, run_duration_s: float, run_id: str = ""
    ) -> tuple[bool, str, str]:
        """Evaluate a run event and decide whether to send a push notification.

        Returns:
            (should_push, message, urgency) where urgency is "high" or "low".
        """
        event_type = self._event_type(event)
        data = self._data(event)

        if event_type in self.HIGH_URGENCY_EVENTS and self.push_on_approval:
            prompt = data.get("prompt") or "Hermes needs your approval"
            logger.debug("Push trigger: %s — '%s'", event_type, prompt)
            return True, str(prompt), "high"

        if event_type in self.LOW_URGENCY_EVENTS and self.push_on_done:
            if run_duration_s >= self.min_run_duration_for_push_s:
                summary = (
                    data.get("summary")
                    or data.get("text")
                    or "Task complete"
                )
                logger.debug(
                    "Push trigger: %s after %.1fs", event_type, run_duration_s
                )
                self._last_progress_at.pop(run_id, None)
                return True, str(summary), "low"
            logger.debug(
                "Skipping push for %s: run only %.1fs (threshold %.1fs)",
                event_type, run_duration_s, self.min_run_duration_for_push_s,
            )
            return False, "", ""

        if event_type in self.PROGRESS_EVENTS and self.push_progress:
            # Only worth waking the phone for genuinely long work.
            if run_duration_s < self.min_run_duration_for_push_s:
                return False, "", ""
            now = time.monotonic()
            last = self._last_progress_at.get(run_id, 0.0)
            if now - last < self.progress_interval_s:
                return False, "", ""
            text = data.get("text") or data.get("message")
            if not text:
                name = data.get("name") or data.get("tool")
                if not name:
                    return False, "", ""
                text = f"Still working — finished {name}."
            self._last_progress_at[run_id] = now
            logger.debug("Push trigger: progress for %s", run_id or "?")
            return True, str(text), "low"

        return False, "", ""
