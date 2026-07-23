"""Herald Relay — Hermes plugin entry point."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from .push_triggers import PushTriggerPolicy
from .relay_client import HeraldRelayClient

logger = logging.getLogger(__name__)


class HeraldRelayPlugin:
    """Hermes plugin that bridges local Hermes to Herald Cloud via a persistent WebSocket tunnel."""

    name = "herald-relay"
    version = "0.1.0"
    description = "Connect your Hermes to Herald — voice-first AI conversations on mobile"

    def __init__(self, config: dict):
        """Initialise the plugin from Hermes config.

        Config keys:
            relay_url (str): Herald Cloud WebSocket URL.
            device_token (str): Push token from the Herald mobile app. Falls back to
                the ``HERALD_DEVICE_TOKEN`` environment variable.
            push_on_approval (bool): Send push when Hermes requires approval (default True).
            push_on_done (bool): Send push when a long run completes (default True).
            min_run_duration_for_push_s (float): Minimum run duration to trigger a done push
                (default 10.0 s — avoids spamming for instant tasks).
        """
        self.relay_url: str = config.get("relay_url", "wss://relay.herald.app")
        self.device_token: str | None = config.get(
            "device_token", os.getenv("HERALD_DEVICE_TOKEN")
        )
        push_on_approval: bool = config.get("push_on_approval", True)
        push_on_done: bool = config.get("push_on_done", True)
        min_duration: float = float(config.get("min_run_duration_for_push_s", 10.0))

        self._push_policy = PushTriggerPolicy(
            push_on_approval=push_on_approval,
            push_on_done=push_on_done,
            min_run_duration_for_push_s=min_duration,
        )

        self.client: HeraldRelayClient | None = None
        self._task: asyncio.Task | None = None

        # Track run start times so we can compute duration for push decisions
        self._run_start_times: dict[str, float] = {}

        if not self.device_token:
            logger.warning(
                "herald-relay: no device_token configured. "
                "Set it in config or via HERALD_DEVICE_TOKEN env var."
            )

    # ------------------------------------------------------------------
    # Hermes plugin lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Called by Hermes when the plugin is activated."""
        local_hermes_url = os.getenv("HERALD_LOCAL_HERMES_URL", "http://localhost:8642")
        self.client = HeraldRelayClient(
            relay_url=self.relay_url,
            device_token=self.device_token or "",
            local_hermes_url=local_hermes_url,
        )
        self._task = asyncio.create_task(
            self.client.run_forever(), name="herald-relay-client"
        )
        logger.info(
            "Herald Relay plugin started (relay=%s, local_hermes=%s).",
            self.relay_url,
            local_hermes_url,
        )

    async def stop(self) -> None:
        """Called by Hermes when the plugin is deactivated."""
        if self.client:
            await self.client.close()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Herald Relay plugin stopped.")

    # ------------------------------------------------------------------
    # Hermes event hook
    # ------------------------------------------------------------------

    async def on_run_event(self, run_id: str, event: dict) -> None:
        """Called by Hermes for each SSE event on a run.

        Used to detect approval_required / completion events and forward
        push triggers to Herald Cloud so the mobile app can wake the user.
        """
        event_type = event.get("type", "")

        # Track when runs start so we can measure duration
        if event_type in ("run_start", "start"):
            self._run_start_times[run_id] = time.monotonic()

        run_start = self._run_start_times.get(run_id, time.monotonic())
        run_duration_s = time.monotonic() - run_start

        should, message, urgency = self._push_policy.should_push(event, run_duration_s)
        if should and self.client:
            logger.info(
                "Sending push trigger for run %s (event=%s, urgency=%s): %s",
                run_id,
                event_type,
                urgency,
                message,
            )
            await self.client.send_push_trigger(
                message=message,
                urgency=urgency,
                metadata={"run_id": run_id, "event_type": event_type},
            )

        # Clean up finished runs
        if event_type in ("run_complete", "final", "run_error"):
            self._run_start_times.pop(run_id, None)
