"""The Herald app asks this computer's Herald Bridge about updates (herald#505).

    GET  [/p/<profile>]/_herald/bridge          {version, auto_update, state, ...}
    POST [/p/<profile>]/_herald/bridge/update   start an update now -> same shape

The app calls ``update`` when a feature answers "too old", shows "Updating
Herald Bridge…" and polls the GET until the version changes (the tunnel
restarts once, so a few polls in between answer device_offline). A build older
than this door hands the call to Hermes, which answers 404: that is how the app
knows this computer can't update itself yet.

Who may ask: whoever holds that profile's API key, checked against local
Hermes exactly like the screen and passwords doors. Answers are always 200 so
the app reads the state ("disabled" for a self-hoster who pinned the version).
"""
from __future__ import annotations

import re

from . import bridge_update
from .local_screen import _caller_owns

_ROUTE = re.compile(
    r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/bridge(?P<rest>/update)?/?$")


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _unmanaged_status() -> dict:
    return {"version": bridge_update.current_version(), "auto_update": False,
            "reason": "not_managed", "state": "not_managed", "target": None,
            "error": None, "last_check": None}


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 updater) -> tuple[int, dict]:
    m = _ROUTE.match((path or "").split("?", 1)[0])
    if not m:
        return 404, {"error": "not_found", "message": "Not found."}
    profile, rest = m.group("profile"), m.group("rest")
    method = (method or "GET").upper()
    if (rest and method != "POST") or (not rest and method != "GET"):
        return 405, {"error": "method_not_allowed", "message": "Not allowed."}
    prefix = f"/p/{profile}" if profile else ""
    if not await _caller_owns(hermes_url, prefix, headers):
        return 401, {"error": "agent_key_required",
                     "message": "That computer didn't accept this agent's key."}
    if updater is None:
        return 200, _unmanaged_status()
    if rest:
        return 200, updater.request_now(force=True)
    return 200, updater.status()
