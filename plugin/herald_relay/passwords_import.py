#!/usr/bin/env python3
"""Import a Chrome password export into this Hermes profile's vault.

    python3 herald_passwords_import.py --profile "Work" --file "Chrome Passwords.csv" [--delete]
    python3 herald_passwords_import.py --count                           # what's saved

Run it with HERMES' OWN interpreter (the one in the `hermes` launcher's
shebang): it imports Hermes' vault store, so the logins land in the same
encrypted vault the browser_vault_* tools fill from.

What it guarantees
  * It prints COUNTS only (added / updated / skipped / total / logins). Never
    a password, never a username, never a site. The agent that runs it sees
    nothing it could repeat.
  * Each login is bound to the exact site it was saved for (scheme://host),
    so browser_vault_fill only ever types it into that site.
  * Re-importing is safe: a login for the same site and username that an
    earlier Chrome import saved is replaced, not duplicated, and keeps its
    authenticator (2FA) key. A login you saved in the vault yourself is never
    overwritten by an export; that row is skipped.
  * All or nothing: the vault is written once, at the end. An import that is
    interrupted leaves the vault exactly as it was.
  * --delete removes the CSV afterwards (overwritten first), whether or not
    the import worked, because a plain-text export left on disk is the real
    leak. Only a file that IS a password export is ever removed: point it at
    the wrong file and that file is left alone.

Shared by Herald Cloud (backend/services/passwords.py sends it to the
machine) and the local plugin (herald-relay). Keep the two copies identical.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import csv
import io
import json
import os
import sys

VERSION = 2
# Chrome writes: name,url,username,password,note  (older: name,url,username,password)
_URL = ("url", "origin", "login_uri", "website")
_USER = ("username", "login_username", "login", "email")
_PASS = ("password", "login_password")
# How a login this tool saved is told apart from one the user saved by hand.
_CHROME_TAG = " (Chrome: "
_BATCH = ("_locked", "_read_all", "_write_all")


def _col(row: dict, names) -> str:
    for n in names:
        v = row.get(n)
        if v:
            return str(v).strip()
    return ""


def is_export(text: str) -> bool:
    """Does the first line name a site column and a password column?"""
    try:
        head = next(csv.reader(io.StringIO(text.lstrip("\ufeff"))), [])
    except csv.Error:
        return False
    cols = {(c or "").strip().lower() for c in head}
    return bool(cols & set(_URL)) and bool(cols & set(_PASS))


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


@contextlib.contextmanager
def _one_write(store):
    """Hermes' vault decrypts, rewrites and fsyncs the whole file on every
    add and remove, so N logins cost N squared: 1,000 took 10 s on an idle
    disk and minutes on a busy one, and an interrupted run left half an
    import behind. Hold the vault's lock once, let its own `add_item` (which
    keeps doing the validation) work on the list in memory, and write once.

    Yields the in-memory record list holder, or None for a store without
    these internals (a different Hermes): the caller then uses the public API
    one item at a time."""
    if not all(callable(getattr(store, n, None)) for n in _BATCH):
        yield None
        return
    with store._locked():
        buf = {"items": list(store._read_all()), "dirty": False}
        real_write = store._write_all
        shadowed = {n: store.__dict__[n] for n in _BATCH
                    if n in getattr(store, "__dict__", {})}

        def write(items):
            buf["items"], buf["dirty"] = items, True

        store._locked = contextlib.nullcontext   # the lock is already ours
        store._read_all = lambda: buf["items"]
        store._write_all = write
        try:
            yield buf
        finally:
            for n in _BATCH:
                if n in shadowed:
                    setattr(store, n, shadowed[n])
                else:
                    delattr(store, n)
        # Reached only when nothing raised: an import that fails writes nothing.
        if buf["dirty"]:
            real_write(buf["items"])


def _saved_logins(store, buf) -> dict:
    """{(origin, identifier): [(id, saved_by_chrome_import, otp_secret)]}"""
    out: dict = {}
    if buf is not None:
        rows = [(r.get("id"), r.get("kind"), r.get("origin"), r.get("identifier"),
                 r.get("label"), (r.get("secret") or {}).get("otp_secret"))
                for r in buf["items"] if isinstance(r, dict)]
    else:
        rows = []
        for m in store.list_items():
            d = m.to_dict() if hasattr(m, "to_dict") else dict(m)
            rows.append((d.get("id"), d.get("kind"), d.get("origin"), d.get("identifier"),
                         d.get("label"), None))
    for rid, kind, origin, ident, label, otp in rows:
        if kind == "login":
            out.setdefault((origin, ident), []).append(
                (rid, _CHROME_TAG in str(label or ""), otp))
    return out


def _otp_of(store, buf, olds) -> str:
    for rid, _, otp in olds:
        if otp:
            return otp
        if buf is None:
            try:
                otp = (store.resolve_secret(rid) or {}).get("otp_secret")
            except Exception:
                otp = None
            if otp:
                return otp
    return ""


def run_import(text: str, profile: str, store=None) -> dict:
    if store is None:
        from agent.vault_store import get_vault_store  # Hermes' own vault
        store = get_vault_store()
    from agent.vault_store import VaultError, normalize_origin

    profile = (profile or "Chrome").strip()[:40] or "Chrome"
    added = updated = skipped = total = 0
    seen = set()
    with _one_write(store) as buf:
        existing = _saved_logins(store, buf)
        drop = set()
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
            olds = existing.get(key, [])
            if any(not chrome for _, chrome, _ in olds):
                skipped += 1          # saved by hand: theirs wins over an export
                continue
            host = origin.split("://", 1)[-1]
            secret = {"identifier_type": "email" if "@" in ident else "username",
                      "identifier": ident, "password": pw}
            otp = _otp_of(store, buf, olds)
            if otp:
                secret["otp_secret"] = otp   # keep the authenticator key
            try:
                store.add_item(kind="login", label=f"{host}{_CHROME_TAG}{profile})",
                               secret=secret, origin=origin)
            except VaultError:
                skipped += 1
                continue
            if olds:
                drop.update(rid for rid, _, _ in olds)
                updated += 1
            else:
                added += 1
        if drop:
            if buf is not None:
                buf["items"] = [r for r in buf["items"]
                                if not (isinstance(r, dict) and r.get("id") in drop)]
                buf["dirty"] = True
            else:
                for rid in drop:
                    try:
                        store.remove_item(rid)
                    except VaultError:
                        pass          # the new login is saved; an old twin stays
    return {"added": added, "updated": updated, "skipped": skipped, "total": total,
            "logins": count(store)["logins"], "version": VERSION}


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
    ap.add_argument("--profile-b64", default="",
                    help="the same label, base64 (Herald Cloud: no user text in a command)")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--file")
    src.add_argument("--count", action="store_true")
    ap.add_argument("--delete", action="store_true", help="remove the CSV afterwards")
    a = ap.parse_args(argv)
    if not a.count and not a.file:
        ap.error("give --file or --count")
    path = os.path.expanduser(a.file) if a.file else ""
    ours = False        # only a real export is ever deleted
    try:
        if a.count:
            out = count()
        else:
            profile = a.profile
            if a.profile_b64:
                profile = base64.b64decode(a.profile_b64).decode("utf-8", "replace")
            with open(path, encoding="utf-8-sig", newline="") as f:
                text = f.read()
            if not is_export(text):
                print(json.dumps({"error": "not_a_password_export"}))
                return 3
            ours = True
            out = run_import(text, profile)
            if a.delete:
                out["deleted_file"] = True
    except FileNotFoundError:
        print(json.dumps({"error": "file_not_found"}))
        return 2
    except UnicodeDecodeError:      # not text at all: not an export, not ours
        print(json.dumps({"error": "not_a_password_export"}))
        return 3
    except Exception as e:  # never echo row content
        print(json.dumps({"error": type(e).__name__}))
        return 1
    finally:
        if path and a.delete and ours:
            _shred(path)
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
