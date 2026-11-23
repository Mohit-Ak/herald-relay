#!/usr/bin/env python3
"""Secure entry, end to end, against a REAL Hermes agent on this machine (herald#398).

    python secure_entry_e2e.py

1. A throwaway sign-in site on 127.0.0.1 whose password exists only in this script.
2. A real Hermes run (local api_server) is asked to sign in; it runs the real
   `herald-tunnel secret request` (this branch) and blocks.
3. This script plays the phone: reads the signed request through the real door
   handler (auth checked against the real Hermes), verifies the Bridge
   signature, encrypts the password exactly as the app does, posts it.
4. The agent fills with browser_vault and signs in. The site records it.
5. Every file under ~/.hermes that could hold it (state.db + WAL, logs,
   sessions, response store), the run's event stream and the spool are
   searched for the password.
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import httpx

PLUGIN = Path(os.environ.get("HERALD_RELAY_PLUGIN", "/home/mohit/projects/wt-398-relay/plugin"))
sys.path.insert(0, str(PLUGIN))
from herald_relay import secure_entry  # noqa: E402

HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
HERMES = os.environ.get("HERMES_URL", "http://127.0.0.1:8642")
TUNNEL = os.environ.get("HERALD_TUNNEL", "/home/mohit/.hermes/cache/scratch/venv398/bin/herald-tunnel")
PORT = int(os.environ.get("SITE_PORT", "8398"))
PASSWORD = "se-" + secrets.token_urlsafe(12)
USER = "ann"
SIGNED_IN: list[str] = []
EVENTS: list[str] = []


def api_key() -> str:
    for line in (HOME / ".env").read_text().splitlines():
        if line.startswith("API_SERVER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no API_SERVER_KEY")


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, html: str, code: int = 200):
        b = html.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self._send("""<!doctype html><title>Acme sign in</title><h1>Acme sign in</h1>
<form method=post action=/login><label>Username <input name=username autocomplete=username></label>
<label>Password <input type=password name=password autocomplete=current-password></label>
<button type=submit>Sign in</button></form>""")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(n).decode())
        u, p = form.get("username", [""])[0], form.get("password", [""])[0]
        if u == USER and p == PASSWORD:
            SIGNED_IN.append(u)
            self._send(f"<h1>Welcome, {u}. You are signed in.</h1>")
        else:
            self._send("<h1>Wrong username or password.</h1>", 401)


async def phone(key: str, stop: asyncio.Event) -> dict:
    """Play the Herald app: wait for the request, verify, encrypt, send."""
    headers = {"Authorization": f"Bearer {key}"}
    while not stop.is_set():
        status, body = await secure_entry.handle("GET", "/_herald/secure-entry", headers, None,
                                                 hermes_url=HERMES, hermes_home=HOME)
        reqs = body.get("requests") or [] if status == 200 else []
        mine = [r for r in reqs if r["origin"] == f"http://127.0.0.1:{PORT}"]
        if mine:
            r = mine[0]
            assert secure_entry.verify(r), "bad signature"
            print(f"  phone: request {r['id'][:8]} for {r['origin']} user={r['username']!r} "
                  f"(signed by Bridge {r['bridge_key'][:10]}…)")
            sealed = secure_entry.seal(r, {"secret": PASSWORD, "username": USER, "save": False})
            assert PASSWORD not in json.dumps(sealed)
            status, body = await secure_entry.handle("POST", f"/_herald/secure-entry/{r['id']}", headers,
                                                     json.dumps(sealed), hermes_url=HERMES, hermes_home=HOME)
            print(f"  phone: sent ciphertext -> {status} {body}")
            return {"request": r, "status": status}
        await asyncio.sleep(0.5)
    return {}


async def main() -> int:
    key = api_key()
    srv = HTTPServer(("127.0.0.1", PORT), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{PORT}"
    prompt = (
        f"Sign in to {origin}/login as '{USER}' in the browser. You don't have the password and the vault "
        f"has no login for it. Ask me for it through Herald secure entry by running exactly this in the "
        f"terminal with timeout 400:\n\n"
        f"{TUNNEL} secret request --home \"{HOME}\" --kind password --origin \"{origin}\" --username \"{USER}\"\n\n"
        f"It prints a vault handle. Then type the username yourself and call browser_vault action=fill with "
        f"that handle and submit=true. Never ask for the password in chat. Tell me the heading shown after "
        f"signing in.")
    async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=None)) as http:
        r = await http.post(f"{HERMES}/v1/runs", headers={"Authorization": f"Bearer {key}"},
                            json={"input": prompt})
        r.raise_for_status()
        run_id = r.json().get("run_id") or r.json().get("id")
        print(f"run {run_id} started")
        stop = asyncio.Event()
        phone_task = asyncio.create_task(phone(key, stop))
        final = ""
        t0 = time.time()
        async with http.stream("GET", f"{HERMES}/v1/runs/{run_id}/events",
                               headers={"Authorization": f"Bearer {key}"}) as s:
            async for line in s.aiter_lines():
                if not line.startswith("data:"):
                    continue
                EVENTS.append(line)
                try:
                    evt = json.loads(line[5:])
                except ValueError:
                    continue
                et = evt.get("event") or evt.get("type")
                if et in ("tool.started", "tool.completed"):
                    print(f"  [{time.time()-t0:5.1f}s] {et} {evt.get('tool')}: {str(evt.get('preview'))[:150]}")
                if et in ("run.completed", "run.failed", "run.cancelled"):
                    final = str(evt.get("output") or evt.get("error") or "")
                    print(f"  [{time.time()-t0:5.1f}s] {et}")
                    break
        stop.set()
        got = await phone_task
    srv.shutdown()
    print("\nagent's answer:", final[:400])
    print("site saw a successful sign-in:", bool(SIGNED_IN))
    print("phone delivered:", bool(got))

    # Where could the password have leaked?
    hits = []
    needle = PASSWORD.encode()
    for sub in ("state.db", "state.db-wal", "response_store.db", "response_store.db-wal", "logs", "sessions",
                "herald", "memories"):
        p = HOME / sub
        files = [p] if p.is_file() else (list(p.rglob("*")) if p.is_dir() else [])
        for f in files:
            try:
                if f.is_file() and needle in f.read_bytes():
                    hits.append(str(f))
            except OSError:
                pass
    if any(PASSWORD in e for e in EVENTS):
        hits.append("run event stream")
    if PASSWORD in final:
        hits.append("agent's answer")
    print("password found in:", hits or "nowhere")
    audit = (HOME / "herald/secure-entry/audit.log").read_text().splitlines()[-5:]
    print("audit (last lines):\n  " + "\n  ".join(audit))
    ok = bool(SIGNED_IN) and bool(got) and not hits
    print("\nRESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
