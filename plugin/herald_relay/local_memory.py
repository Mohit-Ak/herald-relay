"""The Herald app shows and edits this agent's saved memory.

Same door as the screen and password calls (see local_screen.py): the tunnel
client answers, local Hermes never sees the request, and only the holder of
THAT profile's API key is served.

    GET  [/p/<profile>]/_herald/memory   -> {version, stores: {memory, user}}
    POST [/p/<profile>]/_herald/memory   {op: replace|remove, target, id, text?}
                                          -> {ok, version, stores}

Each store is {enabled, limit, used, entries: [{id, text, herald}]}. `herald`
marks Herald's own entry, which is shown but never changed from here.

The work is done by memory_edit.py under Hermes' own interpreter, through
Hermes' MemoryStore (its lock, limits and safety scan). Memory text is never
logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from herald_relay import local_screen

logger = logging.getLogger("herald.memory")

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/memory/?$")
_PROG = Path(__file__).with_name("memory_edit.py")
EDITS_PER_MIN = 30
TIMEOUT_S = 30
_edits: dict[str, list] = {}

_MESSAGES = {
    "changed": (409, "Your agent changed that memory a moment ago. Here is the latest."),
    "herald_entry": (403, "Herald keeps this one up to date. It can't be changed here."),
    "empty": (422, "A memory can't be empty. Delete it instead."),
    "too_long": (422, "That's more than your agent's memory has room for. Make it shorter."),
    "bad_text": (422, "A memory can't contain the § character."),
    "unsafe_text": (422, "Your agent's safety check refused that text. Try different wording."),
    "memory_off": (409, "Memory is turned off for this agent."),
    "memory_unavailable": (409, "This agent doesn't keep memory on this computer."),
    "refused": (422, "Your agent's memory refused that change."),
    "invalid_request": (422, "That request wasn't understood."),
}


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": code, "message": message}


async def _run(argv: list[str], home: Path, job: dict, timeout: float) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, str(_PROG), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=str(home))
    except OSError:
        return {"error": "machine_unavailable"}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"error": "timeout"}
    lines = (out or b"").decode("utf-8", "replace").strip().splitlines()
    try:
        res = json.loads(lines[-1]) if lines else {}
    except ValueError:
        res = {}
    return res if isinstance(res, dict) else {}


def _public(res: dict) -> dict:
    stores = res.get("stores") if isinstance(res.get("stores"), dict) else {}
    return {"version": int(res.get("version") or 1), "stores": stores}


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path, hermes_python) -> tuple[int, dict]:
    m = _ROUTE.match((path or "").split("?", 1)[0])
    if m is None:
        return _err(404, "not_found", "Not found.")
    profile = m.group("profile")
    prefix = f"/p/{profile}" if profile else ""
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    home = local_screen._home(Path(hermes_home), profile)
    if home is None:
        return _err(404, "not_found", "We couldn't find that agent.")
    argv = local_screen.argv_of(hermes_python)
    if not argv:
        return _err(503, "machine_unavailable", "Hermes on that computer couldn't be found.")
    method = (method or "GET").upper()

    if method == "GET":
        job = {"op": "list"}
    elif method == "POST":
        if isinstance(body, str):
            try:
                body = json.loads(body) if body else {}
            except ValueError:
                body = None
        if not isinstance(body, dict):
            return _err(422, "invalid_request", _MESSAGES["invalid_request"][1])
        op, target, eid = body.get("op"), body.get("target"), body.get("id")
        text = body.get("text")
        if (op not in ("replace", "remove") or target not in ("memory", "user")
                or not isinstance(eid, str) or not re.fullmatch(r"[0-9a-f]{16}", eid)
                or (op == "replace" and not isinstance(text, str))):
            return _err(422, "invalid_request", _MESSAGES["invalid_request"][1])
        if op == "replace" and len(text) > 4000:
            return _err(422, "too_long", _MESSAGES["too_long"][1])
        key = str(home)
        now = time.monotonic()
        recent = [t for t in _edits.get(key, []) if now - t < 60]
        if len(recent) >= EDITS_PER_MIN:
            return _err(429, "rate_limited", "Too many changes at once. Wait a moment and try again.")
        _edits[key] = recent + [now]
        job = {"op": op, "target": target, "id": eid}
        if op == "replace":
            job["text"] = text
    else:
        return _err(405, "method_not_allowed", "Method not allowed.")

    res = await _run(argv, home, job, TIMEOUT_S)
    err = res.get("error")
    if err == "machine_unavailable":
        return _err(503, "machine_unavailable", "Hermes on that computer couldn't be found.")
    if err:
        if err in _MESSAGES:
            status, msg = _MESSAGES[err]
            out = {"error": err, "message": msg}
            if err == "changed":       # hand back the fresh list so the app can redraw
                fresh = await _run(argv, home, {"op": "list"}, TIMEOUT_S)
                if "stores" in fresh:
                    out.update(_public(fresh))
            return status, out
        logger.warning("memory %s for %s failed: %s", job["op"], profile or "default",
                       err if re.fullmatch(r"[a-z_]{1,30}", str(err)) else "failed")
        return _err(503, "machine_unavailable", "Couldn't reach this agent's memory on that computer.")
    if "stores" not in res:
        return _err(503, "machine_unavailable", "Couldn't reach this agent's memory on that computer.")
    if job["op"] != "list":
        logger.info("memory entry %s for %s (%s)", "edited" if job["op"] == "replace" else "deleted",
                    profile or "default", job["target"])
    if job["op"] == "list":
        return 200, _public(res)
    return 200, {"ok": True, **_public(res)}
