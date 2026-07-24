"""Herald Relay — Hermes plugin entry point."""
from __future__ import annotations

import asyncio
import logging
import os

from .relay_client import HeraldRelayClient

logger = logging.getLogger(__name__)


class HeraldRelayPlugin:
    """Hermes plugin that bridges local Hermes to Herald Cloud via SSE+POST tunnel."""

    name = "herald-relay"
    version = "0.1.0"
    description = "Connect your Hermes to Herald — voice-first AI conversations on mobile"

    def __init__(self, config: dict):
        """Initialise the plugin from Hermes config.

        Config keys:
            relay_url (str): Herald Cloud base URL  (https://relay.herald.app).
            device_token (str): Push token from the Herald mobile app. Falls back
                to the ``HERALD_DEVICE_TOKEN`` environment variable.
            hermes_version (str): Version string sent to Cloud on connect.
        """
        self.relay_url: str = config.get(
            "relay_url",
            os.getenv("HERALD_RELAY_URL", "https://relay.herald.app"),
        )
        self.device_token: str | None = config.get(
            "device_token", os.getenv("HERALD_DEVICE_TOKEN")
        )
        self.hermes_version: str = config.get("hermes_version", "0.1.0")

        self.client: HeraldRelayClient | None = None
        self._task: asyncio.Task | None = None

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
            hermes_version=self.hermes_version,
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
            await self.client.stop()
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("Herald Relay plugin stopped.")
