#!/usr/bin/env python3
"""Put ONE login into this Hermes profile's vault, or take one out (herald#398).

    echo '{"op":"add","origin":..,"identifier":..,"password":..,"label":..}' | python3 secret_store.py
    echo '{"op":"remove","handle":"vault_…"}'                                | python3 secret_store.py

Run with HERMES' OWN interpreter and HERMES_HOME set (like passwords_import.py):
it uses Hermes' vault store, so browser_vault fills the login, bound to its
origin. The secret arrives on stdin only (never argv, never env, never a file)
and the one JSON line printed carries the handle, never the password.

An earlier login for the same site and username is replaced, so the newest
password wins and the agent sees one handle.
"""
from __future__ import annotations

import json
import sys


def _id_type(identifier: str) -> str:
    if "@" in identifier:
        return "email"
    return "phone" if identifier.lstrip("+").isdigit() else "username"


def main() -> int:
    try:
        job = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        job = {}
    try:
        from agent.vault_store import get_vault_store  # Hermes
    except Exception:  # noqa: BLE001
        print(json.dumps({"error": "vault_unavailable"}))
        return 2
    store = get_vault_store()
    op = job.get("op")
    try:
        if op == "remove":
            handle = str(job.get("handle") or "")
            removed = bool(handle.startswith("vault_") and store.remove_item(handle))
            print(json.dumps({"removed": removed}))
            return 0
        if op != "add":
            print(json.dumps({"error": "bad_op"}))
            return 2
        origin = str(job.get("origin") or "")
        identifier = str(job.get("identifier") or "").strip()
        password = str(job.get("password") or "")
        if not origin or not identifier or not password:
            print(json.dumps({"error": "missing_field"}))
            return 2
        label = str(job.get("label") or "").strip() or origin.split("://", 1)[-1]
        payload = {"identifier_type": _id_type(identifier), "identifier": identifier, "password": password}
        from agent.vault_store import normalize_origin  # Hermes
        olds = [m for m in store.list_items() if m.kind == "login" and m.origin == normalize_origin(origin)
                and (m.identifier or "") == identifier]
        for old in olds:                 # keep the login's authenticator (2FA) key
            if old.has_otp:
                payload["otp_secret"] = str(store.resolve_secret(old.id).get("otp_secret") or "")
                break
        replace = job.get("replace", True) is not False   # a one-time login leaves the saved one alone
        meta = store.add_item("login", label, payload, origin=origin)
        del password
        payload.clear()
        job.clear()
        replaced = sum(int(bool(store.remove_item(old.id))) for old in olds) if replace else 0
        print(json.dumps({"handle": meta.id, "origin": meta.origin, "identifier": identifier,
                          "replaced": replaced}))
        return 0
    except Exception as exc:  # noqa: BLE001 - the message never carries the secret
        print(json.dumps({"error": "vault_error", "type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
