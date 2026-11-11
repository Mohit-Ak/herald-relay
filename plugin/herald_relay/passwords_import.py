#!/usr/bin/env python3
"""Import a Chrome password export into this Hermes profile's vault.

    python3 herald_passwords_import.py --profile "Work" --file "Chrome Passwords.csv" [--delete]
    python3 herald_passwords_import.py --profile "Work" --stdin-b64      # (Herald Cloud)
    python3 herald_passwords_import.py --count                           # what's saved

Run it with HERMES' OWN interpreter (the one in the `hermes` launcher's
shebang): it imports Hermes' vault store, so the logins land in the same
encrypted vault the browser_vault_* tools fill from.

What it guarantees
  * It prints COUNTS only (added / updated / skipped / total). Never a
    password, never a username, never a site. The agent that runs it sees
    nothing it could repeat.
  * Each login is bound to the exact site it was saved for (scheme://host),
    so browser_vault_fill only ever types it into that site.
  * Re-importing is safe: a login for the same site and username is
    replaced, not duplicated.
  * --delete removes the CSV afterwards (overwritten first), because a
    plain-text export left in Downloads is the real leak.

Shared by Herald Cloud (backend/services/passwords.py sends it to the
machine) and the local plugin (herald-relay). Keep the two copies identical.
"""
from __future__ import annotations

import argparse
import base64
import csv
import io
import json
import os
import sys

VERSION = 1
# Chrome writes: name,url,username,password,note  (older: name,url,username,password)
_URL = ("url", "origin", "login_uri", "website")
_USER = ("username", "login_username", "login", "email")
_PASS = ("password", "login_password")


def _col(row: dict, names) -> str:
    for n in names:
        v = row.get(n)
        if v:
            return str(v).strip()
    return ""


def parse(text: str):
    """Yield (origin_url, identifier, password) for each usable row."""
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    for row in reader:
        yield _col(row, _URL), _col(row, _USER), _col(row, _PASS)


def run_import(text: str, profile: str, store=None) -> dict:
    if store is None:
        from agent.vault_store import get_vault_store  # Hermes' own vault
        store = get_vault_store()
    from agent.vault_store import VaultError, normalize_origin

    profile = (profile or "Chrome").strip()[:40] or "Chrome"
    existing = {}
    for m in store.list_items():
        d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
        if d.get("kind") == "login":
            existing[(d.get("origin"), d.get("identifier"))] = d.get("id")

    added = updated = skipped = total = 0
    seen = set()
    for url, ident, pw in parse(text):
        total += 1
        if not url or not ident or not pw or not url.lower().startswith(("http://", "https://")):
            skipped += 1          # Android app logins, blank rows, notes-only rows
            continue
        try:
            origin = normalize_origin(url)
        except Exception:
            skipped += 1
            continue
        key = (origin, ident)
        if key in seen:           # the same login twice in one file
            skipped += 1
            continue
        seen.add(key)
        host = origin.split("://", 1)[-1]
        secret = {"identifier_type": "email" if "@" in ident else "username",
                  "identifier": ident, "password": pw}
        try:
            old = existing.get(key)
            store.add_item(kind="login", label=f"{host} (Chrome: {profile})",
                           secret=secret, origin=origin)
            if old:
                store.remove_item(old)
                updated += 1
            else:
                added += 1
        except VaultError:
            skipped += 1
    return {"added": added, "updated": updated, "skipped": skipped, "total": total,
            "version": VERSION}


def count(store=None) -> dict:
    if store is None:
        from agent.vault_store import get_vault_store
        store = get_vault_store()
    n = 0
    for m in store.list_items():
        d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
        n += d.get("kind") == "login"
    return {"logins": n, "version": VERSION}


def _shred(path: str) -> None:
    try:
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * size)
            f.flush()
            os.fsync(f.fileno())
    except OSError:
        pass
    try:
        os.remove(path)
    except OSError:
        pass


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", default="Chrome", help="label for this Chrome profile")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file")
    src.add_argument("--stdin-b64", action="store_true",
                     help="read a base64 JSON {profile, csv} from stdin")
    src.add_argument("--count", action="store_true")
    ap.add_argument("--delete", action="store_true", help="remove the CSV afterwards")
    a = ap.parse_args(argv)
    try:
        if a.count:
            out = count()
        elif a.stdin_b64:
            payload = json.loads(base64.b64decode(sys.stdin.read().strip()).decode("utf-8"))
            out = run_import(payload.get("csv") or "", payload.get("profile") or a.profile)
        elif a.file:
            with open(os.path.expanduser(a.file), encoding="utf-8-sig", newline="") as f:
                text = f.read()
            out = run_import(text, a.profile)
            if a.delete:
                _shred(os.path.expanduser(a.file))
                out["deleted_file"] = True
        else:
            ap.error("give --file, --stdin-b64 or --count")
    except FileNotFoundError:
        print(json.dumps({"error": "file_not_found"}))
        return 2
    except Exception as e:  # never echo row content
        print(json.dumps({"error": type(e).__name__}))
        return 1
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
