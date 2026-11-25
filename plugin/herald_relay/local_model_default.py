"""Authorize profile-default model changes through the connected Hermes key."""

from __future__ import annotations

from contextlib import suppress

import asyncio
import json
import os
import re
from pathlib import Path

from herald_relay import local_screen

_ROUTE = re.compile(r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/model-default/?$")
_MODEL_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]{0,255}")
_HELPER = Path(__file__).with_name("model_default_helper.py")
_WRITE_LOCK = asyncio.Lock()


def route(path: str):
    return _ROUTE.fullmatch((path or "").split("?", 1)[0])


def matches(path: str) -> bool:
    return route(path) is not None


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": code, "message": message}


def _valid_id(value) -> bool:
    return isinstance(value, str) and _MODEL_ID.fullmatch(value) is not None


async def _run(argv: list[str], home: Path, job: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, str(_HELPER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, cwd=str(home),
        )
    except OSError:
        return {"error": "unsupported"}
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps(job).encode()), timeout=20)
    except TimeoutError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return {"error": "timeout"}
    except asyncio.CancelledError:
        with suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        raise
    try:
        result = json.loads(out.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}
    return result if isinstance(result, dict) else {}


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path, hermes_python) -> tuple[int, dict]:
    match = route(path)
    if match is None:
        return _err(404, "not_found", "Not found.")
    profile = match.group("profile")
    prefix = f"/p/{profile}" if profile else ""
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    home = local_screen._home(Path(hermes_home), profile)
    if home is None:
        return _err(404, "not_found", "We couldn't find that profile.")
    argv = local_screen.argv_of(hermes_python)
    if not argv:
        return _err(501, "unsupported", "Hermes doesn't support this model change.")
    if method == "GET":
        result = await _run(argv, home, {"action": "get"})
    elif method == "PUT":
        if (not isinstance(body, dict) or set(body) - {"model", "provider"}
                or not _valid_id(body.get("model"))
                or ("provider" in body and not _valid_id(body["provider"]))):
            return _err(422, "invalid_model", "Pick a model from this profile's catalog.")
        async with _WRITE_LOCK:
            result = await _run(argv, home, {"action": "set", **body})
    else:
        return _err(405, "method_not_allowed", "Not allowed.")
    error = result.get("error")
    if error == "unsupported":
        return _err(501, error, "Hermes doesn't support this model change.")
    if error == "invalid_model":
        return _err(422, error, "That model is not selectable in this profile.")
    if error == "locked":
        return _err(409, error, "This profile's default model is locked.")
    if error == "timeout":
        return _err(504, error, "Hermes didn't confirm the model operation.")
    if error or result.get("can_set_default") is not True:
        return _err(502, "model_operation_failed", "Hermes couldn't complete the model operation.")
    if method == "GET":
        return 200, {"supported": True}
    return 200, {
        "model": result.get("model") if _valid_id(result.get("model")) else None,
        "provider": result.get("provider") if _valid_id(result.get("provider")) else None,
    }
