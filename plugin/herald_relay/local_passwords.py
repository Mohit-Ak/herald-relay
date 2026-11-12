"""The Herald app adds a Chrome password export to this computer's vault.

Same door as the screen calls (see local_screen.py): the tunnel client answers,
local Hermes never sees the request, and only the holder of THAT profile's API
key is served.

    GET  [/p/<profile>]/_herald/passwords    {logins}
    POST [/p/<profile>]/_herald/passwords    {csv_b64, profile} -> {added, updated, skipped, total, logins}

The export is written to a private file in memory-backed storage when there is
one, imported by Hermes' own interpreter (passwords_import.py) and shredded.
Nothing from it is logged or returned: counts only.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
import re
import tempfile
import time
from pathlib import Path

from herald_relay import local_screen

logger = logging.getLogger("herald.passwords")

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/passwords/?$")
MAX_BYTES = 700 * 1024
IMPORTS_PER_10_MIN = 10
IMPORT_TIMEOUT_S = 240
_PROG = Path(__file__).with_name("passwords_import.py")
_imports: dict[str, list] = {}
_busy: set[str] = set()


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": code, "message": message}


def _private_dir() -> str | None:
    """Memory-backed when the system has it, so the plain text never reaches a disk."""
    for d in ("/dev/shm", os.environ.get("XDG_RUNTIME_DIR") or ""):
        if d and os.path.isdir(d) and os.access(d, os.W_OK):
            return d
    return None


async def _run(py: str, home: Path, args: list[str], timeout: float) -> tuple[int, dict]:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    proc = await asyncio.create_subprocess_exec(
        py, str(_PROG), *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 1, {"error": "timeout"}
    lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
    try:
        res = json.loads(lines[-1]) if lines else {}
    except ValueError:
        res = {}
    return proc.returncode or 0, res if isinstance(res, dict) else {}


def _safe(err) -> str:
    return err if isinstance(err, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,39}", err) else "import_failed"


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path, hermes_python: str) -> tuple[int, dict]:
    m = _ROUTE.match((path or "").split("?", 1)[0])
    if m is None:
        return _err(404, "not_found", "Not found.")
    profile = m.group("profile")
    prefix = f"/p/{profile}" if profile else ""
    # Refuse before the body is even looked at.
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    home = local_screen._home(Path(hermes_home), profile)
    if home is None:
        return _err(404, "not_found", "We couldn't find that agent.")
    if not hermes_python:
        return _err(503, "machine_unavailable", "Hermes on that computer couldn't be found.")
    key = str(home)
    method = (method or "GET").upper()

    if method == "GET":
        code, res = await _run(hermes_python, home, ["--count"], 30)
        if code != 0 or "logins" not in res:
            return _err(503, "machine_unavailable", "Couldn't read the saved logins on that computer.")
        return 200, {"logins": int(res.get("logins") or 0)}
    if method != "POST":
        return _err(405, "method_not_allowed", "Method not allowed.")

    if isinstance(body, str):
        try:
            body = json.loads(body) if body else {}
        except ValueError:
            body = None
    if not isinstance(body, dict) or not isinstance(body.get("csv_b64"), str):
        return _err(422, "invalid_request", "Send the export file.")
    b64 = body["csv_b64"]
    if len(b64) > MAX_BYTES * 4 // 3 + 8:
        return _err(413, "too_large", "That file is too big for a password export (700 KB max).")
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return _err(422, "invalid_request", "Send the export file.")
    if not data or len(data) > MAX_BYTES:
        return _err(413 if data else 422, "too_large" if data else "invalid_request",
                    "That file is too big for a password export (700 KB max)." if data else "That file is empty.")
    label = body.get("profile")
    label = label.strip()[:40] if isinstance(label, str) and label.strip() else "Chrome"

    now = time.monotonic()
    recent = [t for t in _imports.get(key, []) if now - t < 600]
    if len(recent) >= IMPORTS_PER_10_MIN:
        return _err(429, "rate_limited", "Too many imports in a short time. Wait a few minutes and try again.")
    if key in _busy:
        return _err(409, "busy", "An import is already running on that computer.")
    _busy.add(key)
    _imports[key] = recent + [now]
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix="herald-pw-", suffix=".csv", dir=_private_dir())
        with os.fdopen(fd, "wb") as f:          # mkstemp: 0600, ours only
            f.write(data)
        del data, b64, body
        code, res = await _run(hermes_python, home,
                               ["--file", tmp, "--delete", "--profile-b64",
                                base64.b64encode(label.encode()).decode()], IMPORT_TIMEOUT_S)
    finally:
        _busy.discard(key)
        if tmp and os.path.exists(tmp):         # anything but a clean import leaves it: erase it here
            try:
                size = os.path.getsize(tmp)
                with open(tmp, "r+b") as f:
                    f.write(b"\0" * size)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                pass
            try:
                os.unlink(tmp)
            except OSError:
                pass
    if code != 0 or "added" not in res:
        why = _safe(res.get("error"))
        logger.warning("password import for %s failed: %s", profile or "default", why)
        if why == "not_a_password_export":
            return _err(422, "not_a_password_export",
                        "That isn't a Chrome password export. In Chrome: Settings > Password Manager > Export passwords.")
        return _err(502, "import_failed", "The import didn't work on that computer. Nothing was saved.")
    out = {k: int(res.get(k) or 0) for k in ("added", "updated", "skipped", "total", "logins")}
    logger.info("passwords imported for %s: %s", profile or "default", out)
    return 200, out
