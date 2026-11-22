"""The Herald app asks to see this computer's Bot Screen.

The phone reaches the tunnel client over the ordinary keyed proxy path, on an
address local Hermes never sees:

    GET  [/p/<profile>]/_herald/screen           {available, enabled, reason}
    POST [/p/<profile>]/_herald/screen/enable    start it (never installs)
    POST [/p/<profile>]/_herald/screen/setup     the user tapped "Set up screen":
                                                 install + start, in the background,
                                                 only where sudo needs no password
    POST [/p/<profile>]/_herald/screen           {mode} -> {ws_url, mode, expires_in}

Who may ask: whoever holds THAT profile's API key. The relay refuses requests
without a key but does not judge it; we do, by presenting the caller's own
Authorization header to that profile's local Hermes. The machine's key is never
substituted here.

A link is a one-off: a random id, a helper process (screen_pipe.py, run with
Hermes' own interpreter) that dials OUT to the relay and waits 60 s for the
phone. Nothing listens on this computer, and no link or key is logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import sys
import time
from pathlib import Path

import aiohttp

logger = logging.getLogger("herald.screen")

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/screen(?P<rest>/enable|/setup)?/?$")
MODES = ("watch", "control")
LINK_TTL_S = 60
MAX_PIPES_PER_PROFILE = 4
MINTS_PER_10_MIN = 20
_PIPE = Path(__file__).with_name("screen_pipe.py")

# profile home -> live helper processes / last control viewer / mint times
_pipes: dict[str, list] = {}
_control: dict[str, str] = {}
_mints: dict[str, list] = {}


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": code, "message": message}


def _home(root: Path, profile: str | None) -> Path | None:
    if not profile:
        return root
    home = root / "profiles" / profile
    return home if home.is_dir() else None


async def _caller_owns(hermes_url: str, prefix: str, headers: dict) -> bool:
    auth = next((v for k, v in (headers or {}).items() if k.lower() == "authorization" and v), "")
    if not auth.strip() or auth.strip().lower() == "bearer":
        return False
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{hermes_url}{prefix}/v1/models", headers={"Authorization": auth},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception:  # noqa: BLE001
        return False


async def _helper(py, home: Path, job: dict, *, wait_line: float):
    """Start screen_pipe.py for `home`; (process, its first output line)."""
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    proc = await asyncio.create_subprocess_exec(
        *argv_of(py), str(_PIPE), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
    proc.stdin.write(json.dumps(job).encode())
    await proc.stdin.drain()
    proc.stdin.close()
    try:
        line = (await asyncio.wait_for(proc.stdout.readline(), wait_line)).decode("utf-8", "replace").strip()
    except asyncio.TimeoutError:
        line = ""
    return proc, line


async def _status(py, home: Path, start: bool = False) -> dict:
    proc, line = await _helper(py, home, {"op": "start" if start else "status"},
                               wait_line=40 if start else 15)
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except asyncio.TimeoutError:
        proc.kill()
    try:
        return json.loads(line)
    except ValueError:
        return {"error": "status_failed"}


SETUP_STALE_S = 30 * 60


def _setup_state(home: Path) -> dict:
    """The last one-tap setup (screen_pipe.py `setup`): running / failed / done."""
    try:
        st = json.loads((home / "bot-desktop" / "herald-setup.json").read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict):
        return {}
    if st.get("state") == "running":
        pid = st.get("pid")
        alive = False
        if isinstance(pid, int) and pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except PermissionError:
                alive = True
            except OSError:
                alive = False
        if not alive or time.time() - float(st.get("at") or 0) > SETUP_STALE_S:
            return {"state": "failed", "code": "setup_stopped",
                    "message": "Setting up the screen stopped before it finished."}
    return st


def _public(st: dict, setup: dict | None = None) -> dict:
    """What the app shows. `reason` says why a screen can't be opened here;
    `can_set_up` that one tap (POST …/setup) can fix `not_installed`."""
    if st.get("error") == "hermes_too_old":
        reason = "hermes_too_old"
    elif st.get("error"):
        reason = "status_failed"
    elif not st.get("supported"):
        reason = "unsupported_os"
    elif not st.get("installed"):
        reason = "not_installed"
    else:
        reason = None
    setup = setup or {}
    enabled = bool(st.get("running")) and reason is None
    setting_up = setup.get("state") == "running" and not enabled
    setup_failed = setup.get("state") == "failed" and not enabled
    out = {"available": reason is None, "enabled": enabled,
           "enabling": setting_up, "failed": bool(st.get("start_error")) or setup_failed,
           "can_set_up": bool(st.get("can_set_up")) and reason == "not_installed"}
    if reason:
        out["reason"] = reason
    if st.get("start_error"):
        out["message"] = st["start_error"]
    elif setup_failed and setup.get("message"):
        out["message"] = str(setup["message"])[:300]
    return out


async def _setup(py, home: Path) -> tuple[int, dict]:
    """One tap from the app: install (needs password-free sudo) and start, in a
    detached helper. The app polls GET until `enabled` (or `failed`)."""
    if _setup_state(home).get("state") == "running":
        return 200, _public(await _status(py, home), _setup_state(home))
    proc, line = await _helper(py, home, {"op": "setup"}, wait_line=30)
    if line != "started":
        if proc.returncode is None:
            proc.terminate()
        code = line.split(" ", 1)[1] if line.startswith("error ") else "screen_error"
        if code == "needs_password":
            return _err(409, "needs_password",
                        "Setting up the screen needs your password on that computer.")
        if code in ("hermes_too_old", "unsupported_os"):
            return 200, _public({"error": code} if code == "hermes_too_old" else {"supported": False})
        return _err(502, "screen_error", "Couldn't start setting up the screen. Try again.")
    # It keeps going on its own (own session); reap it when it ends.
    asyncio.get_running_loop().create_task(proc.wait())
    logger.info("screen setup started")
    return 200, _public(await _status(py, home), _setup_state(home))


def argv_of(hermes: str | list | tuple | None) -> list[str]:
    """Hermes' interpreter as an argv prefix: a plain path (an older Hermes in a
    venv) or the launcher-derived command (tunnel_cli._hermes_command)."""
    if not hermes:
        return []
    return [hermes] if isinstance(hermes, str) else [str(x) for x in hermes]


def _reap(key: str) -> list:
    live = [p for p in _pipes.get(key, []) if p.returncode is None]
    _pipes[key] = live
    return live


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 relay_url: str, device_token: str, hermes_home: Path,
                 hermes_python) -> tuple[int, dict]:
    m = _ROUTE.match((path or "").split("?", 1)[0])
    if m is None:
        return _err(404, "not_found", "Not found.")
    profile = m.group("profile")
    prefix = f"/p/{profile}" if profile else ""
    if not await _caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    home = _home(Path(hermes_home), profile)
    if home is None:
        return _err(404, "not_found", "We couldn't find that agent.")
    if not hermes_python:
        return 200, _public({"error": "hermes_too_old"})
    key = str(home)
    method = (method or "GET").upper()

    if method == "GET" and not m.group("rest"):
        return 200, _public(await _status(hermes_python, home), _setup_state(home))
    if method != "POST":
        return _err(405, "method_not_allowed", "Method not allowed.")
    if m.group("rest") == "/enable":         # enable = start; never installs
        return 200, _public(await _status(hermes_python, home, start=True))
    if m.group("rest") == "/setup":
        return await _setup(hermes_python, home)

    if isinstance(body, str):
        try:
            body = json.loads(body) if body else {}
        except ValueError:
            body = None
    mode = (body or {}).get("mode", "watch") if isinstance(body, dict) else None
    if mode not in MODES:
        return _err(422, "invalid_request", "Pick watch or control.")
    now = time.monotonic()
    recent = [t for t in _mints.get(key, []) if now - t < 600]
    if len(recent) >= MINTS_PER_10_MIN:
        return _err(429, "rate_limited", "Too many screen links in a short time. Wait a few minutes and try again.")
    st = _public(await _status(hermes_python, home))
    if not st["enabled"]:
        return _err(409, "screen_not_enabled", "The screen is off on that computer. Turn it on first.")

    live = _reap(key)
    while len(live) >= MAX_PIPES_PER_PROFILE:
        old = live.pop(0)
        old.terminate()
    sid = secrets.token_urlsafe(32)
    viewer = "herald-" + secrets.token_urlsafe(12)
    ws_base = relay_url.rstrip("/").replace("https://", "wss://").replace("http://", "ws://")
    job = {"op": "pipe", "relay_ws": f"{ws_base}/tunnel/screen/{sid}", "device_token": device_token,
           "viewer_id": viewer, "mode": mode,
           "release": [_control[key]] if key in _control else []}
    proc, line = await _helper(hermes_python, home, job, wait_line=20)
    if line != "ready":
        if proc.returncode is None:
            proc.terminate()
        code = line.split(" ", 1)[1] if line.startswith("error ") else "screen_error"
        logger.warning("screen link for %s failed: %s", profile or "default", code)
        if code == "not_running":
            return _err(409, "screen_not_enabled", "The screen is off on that computer. Turn it on first.")
        return _err(502, "screen_error", "Couldn't open the screen. Try again.")
    live.append(proc)
    _pipes[key] = live
    _mints[key] = recent + [now]
    if mode == "control":
        _control[key] = viewer
    else:
        _control.pop(key, None)
    logger.info("screen link made for %s (%s)", profile or "default", mode)
    return 200, {"ws_url": f"{ws_base}/hermes/d/{device_token}/_screen/{sid}", "mode": mode,
                 "expires_in": LINK_TTL_S, "stopped_runs": 0}
