"""Push trigger policy — decides when Hermes should wake the user via push notification."""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class PushTriggerPolicy:
    """Decides when Hermes should wake the user via push notification."""

    # High urgency (immediate, with sound):
    HIGH_URGENCY_EVENTS = {"approval_required"}

    # Low urgency (silent banner):
    LOW_URGENCY_EVENTS = {"run_complete", "run_error"}

    def __init__(
        self,
        push_on_approval: bool = True,
        push_on_done: bool = True,
        min_run_duration_for_push_s: float = 10.0,
    ):
        """
        Args:
            push_on_approval: Send high-urgency push when Hermes needs approval.
            push_on_done: Send low-urgency push when a run completes.
            min_run_duration_for_push_s: Only push on completion if the run took
                at least this many seconds (avoids spamming for instant tasks).
        """
        self.push_on_approval = push_on_approval
        self.push_on_done = push_on_done
        self.min_run_duration_for_push_s = min_run_duration_for_push_s

    def should_push(self, event: dict, run_duration_s: float) -> tuple[bool, str, str]:
        """Evaluate a run event and decide whether to send a push notification.

        Returns:
            (should_push, message, urgency) where urgency is "high" or "low".
        """
        event_type = event.get("type", "")

        if event_type == "approval_required" and self.push_on_approval:
            prompt = event.get("data", {}).get("prompt", "Hermes needs your approval")
            logger.debug("Push trigger: approval_required — '%s'", prompt)
            return True, f"⚡ {prompt}", "high"

        if event_type in ("run_complete", "final") and self.push_on_done:
            if run_duration_s >= self.min_run_duration_for_push_s:
                summary = event.get("data", {}).get("summary", "Task complete")
                logger.debug(
                    "Push trigger: %s after %.1fs — '%s'", event_type, run_duration_s, summary
                )
                return True, f"✅ {summary}", "low"
            else:
                logger.debug(
                    "Skipping push for %s: run only %.1fs (threshold %.1fs)",
                    event_type,
                    run_duration_s,
                    self.min_run_duration_for_push_s,
                )

        return False, "", ""
