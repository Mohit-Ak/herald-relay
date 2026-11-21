"""The agent's name (SOUL.md) and skills list, served even when Hermes can't.

Herald reads two things from every agent:

    GET [/p/<profile>]/api/soul     {"soul": "...", ...}  -> the agent's name
    GET [/p/<profile>]/v1/skills    {"object": "list", "data": [...]}

Stock Hermes does not serve the first at all (404), and its /v1/skills 500s
on current releases (it still passes ``include_editorial`` to
``find_all_skills()``, which no longer takes it). Customers used to carry both
fixes as hand-applied patches to ~/.hermes/hermes-agent, and ``hermes update``
stashes local changes, so after every update the app showed "default" instead
of the agent's name and the skills drawer failed (herald#490).

So the tunnel answers these two paths itself when Hermes can't:

1. Ask local Hermes first, with the caller's own key. A 200 is passed through
   untouched: when Hermes serves them (patched, or a future release that fixes
   them), Hermes is the authority. A 401/403 is passed through too.
2. Otherwise (404, 405, 5xx, a body that isn't JSON), check that the caller
   holds THAT profile's key (the same check as the screen routes: present it
   to that profile's ``/v1/models``), then build the answer with Hermes' OWN
   code, run with Hermes' interpreter and that profile's HERMES_HOME
   (``agent_info_helper.py``: ``load_soul_md`` / ``find_all_skills``), so the
   result is what the agent itself sees.
3. If even that fails (a Hermes whose internals moved), read the files
   directly: ``SOUL.md`` and each ``skills/**/SKILL.md`` front matter.

Nothing here writes anything, and the machine's own key is never substituted
for the caller's.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import aiohttp

logger = logging.getLogger("herald.agent_info")

_ROUTE = re.compile(
    r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/(?P<what>api/soul|v1/skills)/?$")
_HELPER = Path(__file__).with_name("agent_info_helper.py")
HELPER_TIMEOUT_S = 30.0
UPSTREAM_TIMEOUT_S = 30.0
# Same caps Hermes' own loaders use in spirit: one persona file, a sane skill tree.
MAX_SOUL_BYTES = 64 * 1024
MAX_SKILLS = 2000
MAX_DESCRIPTION = 1024
# Marks an answer this tunnel built, so a support check can tell the two apart.
SOURCE = "herald-relay"


def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _auth(headers: dict) -> str:
    return next((v for k, v in (headers or {}).items()
                 if k.lower() == "authorization" and v), "")


async def _upstream(hermes_url: str, path: str, auth: str):
    """(status, parsed JSON or None); raises on transport failure."""
    hdrs = {"Accept": "application/json"}
    if auth:
        hdrs["Authorization"] = auth
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{hermes_url}{path}", headers=hdrs,
                            timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S)) as r:
            text = await r.text()
            try:
                return r.status, json.loads(text)
            except ValueError:
                return r.status, None


async def _from_hermes(py: str, home: Path, op: str) -> dict | None:
    """Run agent_info_helper.py with Hermes' interpreter; its JSON, or None."""
    if not py:
        return None
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    try:
        proc = await asyncio.create_subprocess_exec(
            py, str(_HELPER), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env, start_new_session=True)
    except OSError:
        return None
    try:
        out, _ = await asyncio.wait_for(proc.communicate(json.dumps({"op": op}).encode()),
                                        HELPER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        logger.warning("agent info helper timed out (%s)", op)
        return None
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                ans = json.loads(line)
            except ValueError:
                continue
            return ans if isinstance(ans, dict) and "error" not in ans else None
    return None


# ── last resort: the files themselves ───────────────────────────────────────

def soul_from_disk(home: Path) -> str | None:
    p = home / "SOUL.md"
    try:
        with open(p, "rb") as f:
            text = f.read(MAX_SOUL_BYTES).decode("utf-8", "replace").strip()
    except OSError:
        return None
    return text or None


_FM = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)", re.S)


def _front_matter(text: str) -> dict:
    m = _FM.match(text)
    if not m:
        return {}
    try:
        import yaml  # noqa: PLC0415  (a dependency of this package)
        data = yaml.safe_load(m.group(1))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001  a broken header must not drop the whole list
        out = {}
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep and k.strip() in ("name", "description"):
                out[k.strip()] = v.strip().strip("'\"")
        return out


def skills_from_disk(home: Path) -> list[dict]:
    """``{name, description, category}`` for every SKILL.md under <home>/skills."""
    root = home / "skills"
    if not root.is_dir():
        return []
    seen: dict[str, dict] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # Hidden dirs hold Hermes' own bookkeeping (.hub, .archive, ...).
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        if "SKILL.md" not in filenames:
            continue
        rel = Path(dirpath).relative_to(root)
        try:
            with open(Path(dirpath) / "SKILL.md", "rb") as f:
                text = f.read(32 * 1024).decode("utf-8", "replace")
        except OSError:
            continue
        fm = _front_matter(text)
        name = str(fm.get("name") or rel.name).strip()
        if not name or name in seen:
            continue
        desc = fm.get("description")
        desc = " ".join(str(desc).split())[:MAX_DESCRIPTION] if desc else ""
        category = rel.parts[0] if len(rel.parts) > 1 else None
        seen[name] = {"name": name, "description": desc, "category": category}
        if len(seen) >= MAX_SKILLS:
            break
    return sorted(seen.values(), key=lambda s: (s.get("category") or "", s["name"]))


# ── the handler ─────────────────────────────────────────────────────────────

def _home(root: Path, profile: str | None) -> Path | None:
    if not profile:
        return root
    home = root / "profiles" / profile
    return home if home.is_dir() else None


async def _caller_owns(hermes_url: str, prefix: str, auth: str) -> bool:
    if not auth.strip() or auth.strip().lower() == "bearer":
        return False
    try:
        async with aiohttp.ClientSession() as http:
            async with http.get(f"{hermes_url}{prefix}/v1/models", headers={"Authorization": auth},
                                timeout=aiohttp.ClientTimeout(total=10)) as r:
                return r.status == 200
    except Exception:  # noqa: BLE001
        return False


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path, hermes_python: str) -> tuple[int, dict | list]:
    m = _ROUTE.match((path or "").split("?", 1)[0])
    if m is None:
        return 404, {"error": "not_found", "message": "Not found."}
    if (method or "GET").upper() != "GET":
        return 405, {"error": "method_not_allowed", "message": "Method not allowed."}
    hermes_url = hermes_url.rstrip("/")
    profile, what = m.group("profile"), m.group("what")
    prefix = f"/p/{profile}" if profile else ""
    auth = _auth(headers)

    try:
        status, parsed = await _upstream(hermes_url, path, auth)
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent info: local Hermes unreachable: %s", type(exc).__name__)
        return 502, {"error": "hermes_unreachable", "message": "This computer's agent isn't answering."}
    if status == 200 and isinstance(parsed, (dict, list)):
        return 200, parsed
    if status in (401, 403):
        return status, parsed if isinstance(parsed, dict) else {"error": "unauthorized"}

    # Hermes can't answer this one (stock Hermes: 404 soul, 500 skills).
    if not await _caller_owns(hermes_url, prefix, auth):
        return 401, {"error": "agent_key_required", "message": "Send this agent's API key with the request."}
    home = _home(Path(hermes_home), profile)
    if home is None:
        return 404, {"error": "not_found", "message": "We couldn't find that agent."}

    op = "soul" if what == "api/soul" else "skills"
    ans = await _from_hermes(hermes_python, home, op)
    via = "hermes"
    if op == "soul":
        if ans is None or not (ans.get("soul") is None or isinstance(ans.get("soul"), str)):
            soul, via = soul_from_disk(home), "disk"
        else:
            soul = ans.get("soul")
        logger.info("served /api/soul for %s (upstream %s, via %s)", profile or "default", status, via)
        return 200, {"object": "hermes.soul", "profile": profile or "default", "soul": soul,
                     "chars": len(soul) if soul else 0, "source": SOURCE}
    data = ans.get("data") if isinstance(ans, dict) else None
    if not isinstance(data, list):
        data, via = skills_from_disk(home), "disk"
    logger.info("served /v1/skills for %s: %d (upstream %s, via %s)",
                profile or "default", len(data), status, via)
    return 200, {"object": "list", "data": data, "source": SOURCE}
