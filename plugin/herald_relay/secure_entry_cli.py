"""`herald-tunnel secret ...`: the agent's side of secure entry (herald#398).

    herald-tunnel secret request --kind password --origin https://site [--username U] [--label L] [--home H]
    herald-tunnel secret forget --handle vault_… [--after 600] [--home H]

`request` blocks until the user answers on their phone (or gives up) and
prints ONE JSON line for the agent: a vault handle, never the secret. See
secure_entry.py for the protocol and what it protects.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from . import secure_entry

_STORE = Path(__file__).with_name("secret_store.py")
_EPHEMERAL = "ephemeral.json"
FORGET_AFTER_S = 600

_FALLBACK = ("Tell the user you need them to sign in on the screen themselves, and put "
             "[[herald:connect:screen]] on its own at the end of your reply. Never ask for the "
             "password or code in chat.")


def _say(obj: dict) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if obj.get("ok") else 1


def _store(home: Path, job: dict, hermes_cmd: list[str], timeout: float = 120) -> dict:
    """Run secret_store.py with Hermes' interpreter; the job (with the secret) goes on stdin."""
    if not hermes_cmd:
        return {"error": "hermes_not_found"}
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["HERMES_HOME"] = str(home)
    data = json.dumps(job).encode()
    job.clear()
    try:
        r = subprocess.run([*hermes_cmd, str(_STORE)], input=data, capture_output=True,
                           env=env, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return {"error": "vault_error"}
    finally:
        del data
    lines = (r.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    try:
        out = json.loads(lines[-1]) if lines else {}
    except ValueError:
        out = {}
    return out if isinstance(out, dict) else {}


def _ephemeral(home: Path) -> dict:
    try:
        d = json.loads((secure_entry.spool(home) / _EPHEMERAL).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _set_ephemeral(home: Path, d: dict) -> None:
    secure_entry._write_private(secure_entry.spool(home) / _EPHEMERAL, json.dumps(d).encode())


def sweep(home: Path, hermes_cmd: list[str], *, now: float | None = None) -> int:
    """Remove one-time logins whose time is up (a forget that never ran: reboot, kill)."""
    now = now if now is not None else time.time()
    eph = _ephemeral(home)
    due = [h for h, t in eph.items() if float(t) <= now]
    for h in due:
        _store(home, {"op": "remove", "handle": h}, hermes_cmd)
        eph.pop(h, None)
        secure_entry.audit(home, f"forgot {h}")
    if due:
        _set_ephemeral(home, eph)
    return len(due)


def _spawn_forget(home: Path, handle: str, after: int) -> None:
    try:
        subprocess.Popen(
            [sys.executable, "-m", "herald_relay.tunnel_cli", "secret", "forget", "--handle", handle,
             "--after", str(after), "--home", str(home)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
    except OSError:
        pass                            # the next request's sweep removes it


def request(home: Path, kind: str, origin: str, username: str, label: str, hermes_cmd: list[str],
            *, seen_timeout: float = secure_entry.SEEN_TIMEOUT_S, wait=secure_entry.wait_answer) -> dict:
    if kind != "password":
        # A code has to be typed into the page by Hermes itself; the stock api_server has no
        # way to take one from outside its process yet (see the contract doc).
        return {"ok": False, "error": "unsupported_kind", "next": _FALLBACK}
    try:
        req, sk = secure_entry.create_request(home, kind, origin, username, label)
    except ValueError as exc:
        return {"ok": False, "error": "invalid_request", "message": str(exc)}
    sweep(home, hermes_cmd)
    got = wait(home, req, seen_timeout=seen_timeout)
    if "answer" not in got:
        why = got.get("error") or "timed_out"
        if why == "app_not_reached":
            return {"ok": False, "error": why, "next": "The Herald app didn't pick this up (it may be "
                    "closed, or too old). " + _FALLBACK}
        return {"ok": False, "error": why, "next": "Nobody answered in time. Ask the user if they "
                "still want to sign in before trying again."}
    answer = got["answer"]
    if answer.get("declined"):
        return {"ok": False, "error": "declined",
                "next": "The user chose not to enter it. Don't ask again this turn."}
    try:
        plain = secure_entry.open_answer(sk, req, answer)
    except ValueError:
        secure_entry.audit(home, f"undecryptable {req['id'][:8]}")
        return {"ok": False, "error": "bad_answer", "next": _FALLBACK}
    finally:
        del sk
    identifier = str(plain.get("username") or req.get("username") or "").strip()
    save = plain.get("save") is not False
    res = _store(home, {"op": "add", "origin": req["origin"], "identifier": identifier,
                        "password": plain.pop("secret"), "label": req["label"], "replace": save},
                 hermes_cmd)
    plain.clear()
    if not res.get("handle"):
        secure_entry.audit(home, f"store_failed {req['id'][:8]} {res.get('error') or 'unknown'}")
        return {"ok": False, "error": res.get("error") or "vault_error", "next": _FALLBACK}
    handle = res["handle"]
    if not save:
        eph = _ephemeral(home)
        eph[handle] = time.time() + FORGET_AFTER_S
        _set_ephemeral(home, eph)
        _spawn_forget(home, handle, FORGET_AFTER_S)
    secure_entry.audit(home, f"stored {req['id'][:8]} {handle} saved={'yes' if save else 'no'}")
    return {"ok": True, "handle": handle, "origin": req["origin"], "identifier": identifier,
            "saved": save,
            "next": (f"Type the username yourself if the form asks for it, then call browser_vault "
                     f"action=fill handle={handle}. It only fills on {req['origin']}."
                     + ("" if save else " This login is removed in 10 minutes."))}


def forget(home: Path, handle: str, after: float, hermes_cmd: list[str], sleep=time.sleep) -> dict:
    if after > 0:
        sleep(after)
    res = _store(home, {"op": "remove", "handle": handle}, hermes_cmd)
    eph = _ephemeral(home)
    if eph.pop(handle, None) is not None:
        _set_ephemeral(home, eph)
    secure_entry.audit(home, f"forgot {handle}")
    return {"ok": True, "removed": bool(res.get("removed"))}


def add_parser(sub) -> None:
    p = sub.add_parser("secret", help="ask the user for one password in the Herald app")
    p.add_argument("action", choices=["request", "forget"])
    p.add_argument("--kind", default="password", choices=list(secure_entry.KINDS))
    p.add_argument("--origin", default="")
    p.add_argument("--username", default="")
    p.add_argument("--label", default="")
    p.add_argument("--handle", default="")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--home", default="", help="the Hermes profile home (default: HERMES_HOME)")
    p.set_defaults(func=cmd_secret)


def cmd_secret(args: argparse.Namespace) -> int:
    from . import tunnel_cli  # noqa: PLC0415
    home = Path(args.home) if args.home else tunnel_cli.HERMES_HOME
    cmd = tunnel_cli._hermes_command()
    if args.action == "forget":
        if not args.handle:
            return _say({"ok": False, "error": "handle_required"})
        return _say(forget(home, args.handle, args.after, cmd))
    return _say(request(home, args.kind, args.origin, args.username, args.label, cmd))
