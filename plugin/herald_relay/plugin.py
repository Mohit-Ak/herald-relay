"""Herald Relay — Hermes plugin entry point."""
from __future__ import annotations

import asyncio
import logging
import os

from .relay_client import HeraldRelayClient

logger = logging.getLogger(__name__)


def _api_key_from_env_file() -> str:
    """Read API_SERVER_KEY from ~/.hermes/.env as a last resort.

    The gateway loads .env into its own process env, but the plugin may be
    constructed in a context where that has not happened yet (e.g. a CLI
    probe). Reading the file directly keeps the tunnel usable either way.
    """
    try:
        from pathlib import Path

        env_path = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes")) / ".env"
        if not env_path.is_file():
            return ""
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:  # pragma: no cover - best effort
        logger.debug("herald-relay: could not read API_SERVER_KEY", exc_info=True)
    return ""


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
        # Key for the LOCAL Hermes api_server. Config wins; otherwise fall back
        # to the same API_SERVER_KEY the gateway itself reads from the
        # environment / ~/.hermes/.env, so a working api_server needs no extra
        # setup here.
        self.hermes_key: str = config.get(
            "hermes_key", os.getenv("API_SERVER_KEY", "")
        ) or _api_key_from_env_file()

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
            hermes_key=self.hermes_key,
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


# ---------------------------------------------------------------------------
# Hermes plugin registration
# ---------------------------------------------------------------------------
#
# Hermes loads a plugin by importing its entry-point target and calling a
# module-level ``register(ctx)`` (see hermes_cli/plugins.py ``_load_plugin``).
# Without it the plugin is discovered, reported as "enabled", and then never
# loads -- the manager records ``error="no register() function"`` and moves on.
#
# The tunnel is a long-lived asyncio task, but ``register()`` is synchronous and
# may run before any event loop exists (CLI startup) or inside a running loop
# (gateway). So we do NOT start the tunnel here. We bind it to the session
# lifecycle hooks and start it lazily on the first ``on_session_start``, which
# is guaranteed to run inside the gateway's event loop.

_PLUGIN: HeraldRelayPlugin | None = None


def _load_plugin_config() -> dict:
    """Read ``herald.*`` settings from config.yaml.

    Falls back to an empty dict so the plugin degrades to env vars
    (HERALD_RELAY_URL / HERALD_DEVICE_TOKEN) rather than raising at load time.
    """
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        section = cfg.get("herald")
        return section if isinstance(section, dict) else {}
    except Exception:  # pragma: no cover - config is optional
        logger.debug("herald-relay: could not read config.yaml", exc_info=True)
        return {}


def _spawn(coro) -> None:
    """Run *coro* whether or not we are already on an event loop.

    Hermes invokes hooks SYNCHRONOUSLY (``ret = cb(**kwargs)`` in
    PluginManager.invoke_hook). An ``async def`` hook therefore returns an
    un-awaited coroutine that is silently dropped -- the tunnel never starts
    and nothing is logged. Hooks must be plain functions that schedule their
    own async work.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None:
        loop.create_task(coro)
        return

    # No running loop (CLI startup): run to completion in a private loop.
    try:
        asyncio.run(coro)
    except Exception:
        logger.exception("herald-relay: background task failed")


async def _start_tunnel() -> None:
    if _PLUGIN is None:
        return
    if _PLUGIN._task is not None and not _PLUGIN._task.done():
        return  # already running
    try:
        await _PLUGIN.start()
    except Exception:
        logger.exception("herald-relay: tunnel failed to start")


async def _stop_tunnel() -> None:
    if _PLUGIN is None:
        return
    try:
        await _PLUGIN.stop()
    except Exception:
        logger.exception("herald-relay: tunnel failed to stop cleanly")


def _on_session_start(**_kwargs) -> None:
    """Start the relay tunnel once, inside a live event loop.

    NOTE: deliberately a *sync* function -- see _spawn().
    """
    if _PLUGIN is None:
        return
    if _PLUGIN._task is not None and not _PLUGIN._task.done():
        return
    if not _PLUGIN.device_token:
        # Registering the device in the Herald app is what mints this token.
        logger.warning(
            "herald-relay: no device_token configured — tunnel not started. "
            "Register the device in the Herald app, then set herald.device_token."
        )
        return
    logger.info("herald-relay: starting tunnel to %s", _PLUGIN.relay_url)
    _spawn(_start_tunnel())


def _on_session_end(**_kwargs) -> None:
    """Tear the tunnel down with the session."""
    if _PLUGIN is None:
        return
    _spawn(_stop_tunnel())


def register(ctx) -> None:
    """Hermes plugin entry point."""
    global _PLUGIN
    _PLUGIN = HeraldRelayPlugin(_load_plugin_config())
    ctx.register_hook("on_session_start", _on_session_start)
    ctx.register_hook("on_session_end", _on_session_end)
    logger.info(
        "herald-relay registered (relay=%s, token=%s)",
        _PLUGIN.relay_url,
        "set" if _PLUGIN.device_token else "MISSING",
    )
