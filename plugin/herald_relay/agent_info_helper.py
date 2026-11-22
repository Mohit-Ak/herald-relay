"""The agent's identity and skills, computed by Hermes' own code.

Run by the tunnel client with HERMES' OWN interpreter and the profile's home:

    HERMES_HOME=<profile home>  <hermes python>  agent_info_helper.py  < {"op": "soul"|"skills"}

Prints one JSON line:

    {"soul": "<SOUL.md as the agent loads it>" | null}
    {"data": [{"name", "description", "category"}, ...]}
    {"error": "<code>"}                      (the caller then reads the files)

Using Hermes' loaders, not a copy, keeps the answer identical to what the
agent itself sees: the same SOUL.md scanning and truncation, the same skill
shadowing, disabled state and external skill dirs. ``find_all_skills`` is
called with only the keyword arguments this Hermes accepts, which is exactly
the bug that breaks Hermes' own GET /v1/skills (herald#490).
"""
from __future__ import annotations

import inspect
import json
import logging
import sys

logging.disable(logging.CRITICAL)  # nothing but the answer on stdout


def _hermes_on_path() -> None:
    """Put Hermes' source tree first on sys.path.

    Hermes is usually an editable install, and an editable finder only maps
    the top-level modules that existed when it was installed. After a
    `hermes update` adds one (seen: `hermes_yaml`), importing Hermes' code
    from outside its tree fails with ModuleNotFoundError. Hermes itself runs
    from its tree, so do the same.
    """
    import importlib.util
    import os
    from pathlib import Path
    roots = []
    try:
        spec = importlib.util.find_spec("tools")
        if spec and spec.submodule_search_locations:
            roots.append(Path(list(spec.submodule_search_locations)[0]).parent)
    except Exception:  # noqa: BLE001
        pass
    roots.append(Path(os.environ.get("HERMES_AGENT_DIR") or Path.home() / ".hermes" / "hermes-agent"))
    for root in roots:
        if (root / "tools" / "skills_tool.py").is_file():
            if str(root) not in sys.path:
                sys.path.insert(0, str(root))
            return


def say(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    return 0


def soul() -> dict:
    from agent.prompt_builder import load_soul_md
    text = load_soul_md()
    return {"soul": text if isinstance(text, str) and text.strip() else None}


def skills() -> dict:
    from tools import skills_tool
    find = skills_tool.find_all_skills
    params = inspect.signature(find).parameters
    kw = {}
    if "skip_disabled" in params:
        kw["skip_disabled"] = False
    if "include_editorial" in params:
        kw["include_editorial"] = True
    found = find(**kw)
    sort = getattr(skills_tool, "sort_skills", None)
    if callable(sort):
        found = sort(found)
    data = [{"name": s.get("name"), "description": s.get("description") or "",
             "category": s.get("category")}
            for s in found if isinstance(s, dict) and s.get("name")]
    return {"data": data}


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return say({"error": "bad_job"})
    op = job.get("op")
    _hermes_on_path()
    try:
        if op == "soul":
            return say(soul())
        if op == "skills":
            return say(skills())
    except ImportError:
        return say({"error": "hermes_too_old"})
    except Exception as exc:  # noqa: BLE001
        return say({"error": type(exc).__name__})
    return say({"error": "bad_op"})


if __name__ == "__main__":
    sys.exit(main())
