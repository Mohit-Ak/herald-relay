#!/usr/bin/env python3
"""Standalone Herald relay tunnel runner.

Runs the herald-relay SSE tunnel as its own long-lived process, independent of
Hermes session lifecycle.

WHY THIS EXISTS
---------------
The plugin originally started the tunnel from the ``on_session_start`` hook,
which Hermes fires only when a *brand-new* conversation is created. That made
remote access silently dependent on chat activity: a gateway restart alone left
the tunnel down, and a long-running conversation never re-armed it. Remote
access to your machine should not depend on whether you happen to be chatting.

This runner owns the tunnel directly and is supervised by systemd, so it comes
up at boot, restarts on failure, and reconnects with backoff on network loss.

Config is read from the same places the plugin uses:
  herald.relay_url / herald.device_token in ~/.hermes/config.yaml
  API_SERVER_KEY in ~/.hermes/.env  (auth for the local Hermes api_server)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/mohit/projects/herald-relay/plugin")

from herald_relay.relay_client import HeraldRelayClient  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("herald-tunnel")

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))


def _config() -> dict:
    """Read the herald.* section from config.yaml."""
    try:
        import yaml

        cfg = yaml.safe_load((HERMES_HOME / "config.yaml").read_text()) or {}
        section = cfg.get("herald")
        return section if isinstance(section, dict) else {}
    except Exception:
        logger.warning("could not read config.yaml", exc_info=True)
        return {}


def _api_key() -> str:
    """API_SERVER_KEY for the local Hermes api_server."""
    env_path = HERMES_HOME / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip()
    except Exception:
        logger.warning("could not read %s", env_path)
    return ""


async def main() -> int:
    cfg = _config()
    relay_url = cfg.get("relay_url") or os.getenv("HERALD_RELAY_URL", "")
    device_token = cfg.get("device_token") or os.getenv("HERALD_DEVICE_TOKEN", "")
    local_hermes = os.getenv("HERALD_LOCAL_HERMES_URL", "http://127.0.0.1:8642")
    key = _api_key()

    if not relay_url or not device_token:
        logger.error(
            "missing config: set herald.relay_url and herald.device_token "
            "in %s/config.yaml",
            HERMES_HOME,
        )
        return 1

    logger.info(
        "starting tunnel relay=%s local_hermes=%s api_key=%s",
        relay_url,
        local_hermes,
        "set" if key else "MISSING",
    )

    client = HeraldRelayClient(
        relay_url=relay_url,
        device_token=device_token,
        local_hermes_url=local_hermes,
        hermes_key=key,
    )
    # run_forever() already handles reconnect with exponential backoff.
    await client.run_forever()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
