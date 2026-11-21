#!/usr/bin/env python3
"""Read or change this Hermes profile's saved memory (MEMORY.md / USER.md).

Run it with HERMES' OWN interpreter and HERMES_HOME set to the profile. The
job arrives on stdin as JSON (never in argv, which other users can read):

    {"op": "list"}
    {"op": "replace", "target": "user"|"memory", "id": "<entry id>", "text": "..."}
    {"op": "remove",  "target": "user"|"memory", "id": "<entry id>"}

It answers with ONE JSON line. Edits go through Hermes' own MemoryStore, so
they take its file lock, its size limits and its safety scan exactly as the
agent's memory tool does. An entry is named by a hash of its text; the edit is
pinned to that exact entry (`matched_entry`), so an entry the agent changed in
the meantime is refused ("changed") rather than overwritten.

Herald's own entry (`Herald account: ...`, written when a Herald Cloud agent
is created) can be read but never edited, removed or forged from here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

VERSION = 1
TARGETS = ("memory", "user")
HERALD_PREFIX = "Herald account:"
MAX_TEXT = 2000


def entry_id(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def is_herald(text: str) -> bool:
    return text.strip().startswith(HERALD_PREFIX)


def _out(obj: dict) -> int:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    return 0


def _import_memory():
    """Hermes' memory module. Some installs only expose the packages their
    editable finder knew about at install time; the source root fixes that."""
    try:
        from tools import memory_tool  # noqa: PLC0415
        return memory_tool
    except Exception:  # noqa: BLE001
        import hermes_cli  # noqa: PLC0415
        root = os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        for name in [m for m in sys.modules if m == "tools" or m.startswith("tools.")]:
            del sys.modules[name]
        from tools import memory_tool  # noqa: PLC0415
        return memory_tool


def _hosted_off() -> bool:
    try:
        from tools.hosted_tool_policy import host_feature_enabled  # noqa: PLC0415
        return not host_feature_enabled("builtin_memory")
    except Exception:  # noqa: BLE001 - older Hermes has no hosted policy
        return False


def _entries(store, target: str) -> list[str]:
    return list(store.user_entries if target == "user" else store.memory_entries)


def listing(store) -> dict:
    out = {"version": VERSION, "stores": {}}
    for t in TARGETS:
        entries = _entries(store, t)
        enabled = store.target_enabled(t) if hasattr(store, "target_enabled") else True
        limit = store.user_char_limit if t == "user" else store.memory_char_limit
        used = len("\n§\n".join(entries))
        out["stores"][t] = {
            "enabled": bool(enabled), "limit": int(limit), "used": used,
            "entries": [{"id": entry_id(e), "text": e, "herald": is_herald(e)} for e in entries]}
    return out


def _edit(store, method: str, target: str, entry: str, text: str | None) -> dict:
    fn = getattr(store, method)
    args = (target, entry) if text is None else (target, entry, text)
    try:
        return fn(*args, matched_entry=entry)
    except TypeError:  # an older Hermes without pinned edits: the whole entry still matches exactly
        return fn(*args)


def run(job: dict) -> dict:
    mt = _import_memory()
    if _hosted_off():
        return {"error": "memory_unavailable"}
    store = mt.load_on_disk_store()
    op = job.get("op")
    if op == "list":
        return listing(store)
    if op not in ("replace", "remove"):
        return {"error": "invalid_request"}
    target = job.get("target")
    if target not in TARGETS:
        return {"error": "invalid_request"}
    if hasattr(store, "target_enabled") and not store.target_enabled(target):
        return {"error": "memory_off"}
    want = job.get("id")
    match = next((e for e in _entries(store, target) if entry_id(e) == want), None)
    if match is None:
        return {"error": "changed"}
    if is_herald(match):
        return {"error": "herald_entry"}
    if op == "remove":
        res = _edit(store, "remove", target, match, None)
    else:
        text = job.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"error": "empty"}
        text = text.strip()
        if len(text) > MAX_TEXT:
            return {"error": "too_long"}
        if "§" in text:
            return {"error": "bad_text"}
        if is_herald(text):
            return {"error": "herald_entry"}
        if text == match:
            return {"ok": True, **listing(store)}
        res = _edit(store, "replace", target, match, text)
    if not isinstance(res, dict) or not res.get("success"):
        err = str((res or {}).get("error") or "")
        low = err.lower()
        cls = ""
        try:
            cls = str(mt.FAILURE_CLASS.get())
        except Exception:  # noqa: BLE001 - older Hermes
            pass
        if cls == "scan_blocked":
            return {"error": "unsafe_text"}
        if cls == "over_budget":
            return {"error": "too_long"}
        if cls in ("stale_entry", "no_match", "ambiguous"):
            return {"error": "changed"}
        if "limit" in low or "exceed" in low:
            return {"error": "too_long"}
        if "no longer" in low or "stale" in low or "no entry" in low or "matched" in low:
            return {"error": "changed"}
        if low:
            return {"error": "refused"}
        return {"error": "failed"}
    fresh = mt.load_on_disk_store()
    return {"ok": True, **listing(fresh)}


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return _out({"error": "invalid_request"})
    if not isinstance(job, dict):
        return _out({"error": "invalid_request"})
    try:
        return _out(run(job))
    except Exception as exc:  # noqa: BLE001 - one JSON line, never a traceback with memory in it
        return _out({"error": "failed", "kind": type(exc).__name__})


if __name__ == "__main__":
    sys.exit(main())
