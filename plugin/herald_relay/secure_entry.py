"""Secure entry: the agent asks for ONE secret, the user types it in the Herald app (herald#398).

The secret is encrypted on the phone to a key that exists only in the memory
of the waiting `herald-tunnel secret request` process on this computer. The
relay, Herald's servers, the tunnel process and the agent only ever carry
ciphertext; the plain text exists on the phone and, for a moment, in that
one process, which hands it straight to Hermes' vault (bound to the site)
and prints only the vault handle.

    agent:  herald-tunnel secret request --kind password --origin https://site
              -> writes <id>.req.json (public key + signed by this Bridge), waits
    app:    GET  [/p/<profile>]/_herald/secure-entry        {requests: [...]}
            POST [/p/<profile>]/_herald/secure-entry/<id>   {epk, nonce, ct} | {decline: true}
              -> the tunnel writes <id>.ans.json (ciphertext only)
    agent:  the waiting process decrypts, stores the login, prints {handle}
            then browser_vault action=fill handle=<handle>

Crypto (both ends: here and flutter/lib/services/secure_entry.dart):
  * Each request has a fresh X25519 key pair; the private half never leaves
    the waiting process (never written anywhere).
  * The phone: ephemeral X25519, shared = X25519(e, pk),
    key = HKDF-SHA256(shared, salt = epk || pk, info = "herald-secure-entry/v1 key", 32),
    AES-256-GCM with a random 96-bit nonce and AAD = transcript(request).
  * The request is signed (Ed25519) by this Bridge's long-lived identity key
    (<home>/herald/bridge-identity.key, 0600). The app pins that key per
    agent the first time it sees it (trust on first use) and refuses a
    request signed by any other key, so a relay or server that swaps in its
    own public key is caught.

What this does NOT protect against: other code running as the same OS user
as Hermes (the agent's own terminal included) can read the identity key and
the vault key, exactly like Hermes' own vault. Secure entry keeps secrets out
of the model's context, transcripts, logs and Herald's servers; it is not a
sandbox. See docs/internal/plans/secure-entry-contract.md in the herald repo.
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 1
KINDS = ("password", "code")
TTL_S = {"password": 300, "code": 600}   # password: under a terminal tool's 600 s cap
SEEN_TIMEOUT_S = 120            # the app must pick a request up this soon, or the agent falls back
MAX_CT_B64 = 4096
MAX_FIELD = 200
_INFO = b"herald-secure-entry/v1 key"
_TAG = "herald-secure-entry/v1"
_ID = re.compile(r"^[a-f0-9]{32}$")
_ROUTE = re.compile(
    r"^(?:/p/(?P<profile>[A-Za-z0-9][A-Za-z0-9_.-]{0,63}))?/_herald/secure-entry"
    r"(?:/(?P<rid>[a-f0-9]{32}))?/?$")
_ORIGIN = re.compile(r"^https?://[A-Za-z0-9.-]{1,253}(?::\d{1,5})?$")


# ── encoding ────────────────────────────────────────────────────────────

def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def b64d(s: str) -> bytes:
    if not isinstance(s, str) or len(s) > MAX_CT_B64 * 2 or not re.fullmatch(r"[A-Za-z0-9_-]*", s):
        raise ValueError("bad base64url")
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def normalize_origin(url: str) -> str:
    """scheme://host[:port], lower-cased; ValueError for anything else (paths are dropped)."""
    from urllib.parse import urlsplit
    if not isinstance(url, str) or not url.strip():
        raise ValueError("origin required")
    u = urlsplit(url.strip() if "://" in url else "https://" + url.strip())
    if u.scheme not in ("https", "http") or not u.hostname:
        raise ValueError("origin must be an http(s) site")
    host = u.hostname.lower()
    port = f":{u.port}" if u.port and not ((u.scheme == "https" and u.port == 443)
                                           or (u.scheme == "http" and u.port == 80)) else ""
    origin = f"{u.scheme}://{host}{port}"
    if not _ORIGIN.match(origin):
        raise ValueError("origin must be an http(s) site")
    return origin


def transcript(req: dict) -> bytes:
    """The bytes that are signed and used as AEAD associated data. Text fields are
    base64url-encoded so both ends build exactly the same bytes."""
    def t(v) -> str:
        return b64e((v or "").encode("utf-8"))
    return "\n".join([
        _TAG, req["id"], req["kind"], t(req.get("origin")), t(req.get("username")),
        t(req.get("label")), str(int(req["created_at"])), str(int(req["expires_at"])), req["pk"],
    ]).encode("utf-8")


# ── files ───────────────────────────────────────────────────────────────

def spool(home: Path) -> Path:
    d = Path(home) / "herald" / "secure-entry"
    d.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(d.parent, 0o700)
        os.chmod(d, 0o700)
    except OSError:
        pass
    return d


def _write_private(path: Path, data: bytes, *, exclusive: bool = False) -> None:
    """0600 file written atomically (tmp + rename), or created exclusively (O_EXCL)."""
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def identity(home: Path) -> Ed25519PrivateKey:
    """This Bridge's long-lived signing key for one Hermes profile (created on first use)."""
    p = Path(home) / "herald" / "bridge-identity.key"
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        return Ed25519PrivateKey.from_private_bytes(p.read_bytes())
    except (OSError, ValueError):
        pass
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    try:
        _write_private(p, raw, exclusive=True)
    except FileExistsError:             # another process won the race: use its key
        return Ed25519PrivateKey.from_private_bytes(p.read_bytes())
    return key


def _pub_raw(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def audit(home: Path, line: str) -> None:
    """One line per event, never a value: when, what, which site."""
    try:
        p = spool(home) / "audit.log"
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{stamp} {line}\n")
    except OSError:
        pass


# ── the waiting side (herald-tunnel secret request) ─────────────────────

def create_request(home: Path, kind: str, origin: str, username: str = "", label: str = "",
                   *, now: float | None = None) -> tuple[dict, X25519PrivateKey]:
    """Write a signed pending request; returns (request, private key). The key is
    only ever in the caller's memory."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    origin = normalize_origin(origin)
    username = (username or "").strip()[:MAX_FIELD]
    label = (label or "").strip()[:60] or origin.split("://", 1)[1]
    now = int(now if now is not None else time.time())
    sk = X25519PrivateKey.generate()
    req = {"v": VERSION, "id": secrets.token_hex(16), "kind": kind, "origin": origin,
           "username": username, "label": label, "created_at": now,
           "expires_at": now + TTL_S[kind], "pk": b64e(_pub_raw(sk))}
    ident = identity(home)
    req["bridge_key"] = b64e(_pub_raw(ident))
    req["sig"] = b64e(ident.sign(transcript(req)))
    d = spool(home)
    _write_private(d / f"{req['id']}.req.json", json.dumps(req).encode())
    audit(home, f"request {req['id'][:8]} {kind} {origin}")
    return req, sk


def _state(home: Path, rid: str) -> dict:
    d = spool(home)
    return {"seen": (d / f"{rid}.seen").exists(), "answer": d / f"{rid}.ans.json"}


def cleanup(home: Path, rid: str) -> None:
    d = spool(home)
    for suffix in (".req.json", ".seen", ".ans.json"):
        try:
            (d / f"{rid}{suffix}").unlink()
        except OSError:
            pass


def wait_answer(home: Path, req: dict, *, seen_timeout: float = SEEN_TIMEOUT_S,
                poll: float = 0.5, clock=time.time, sleep=time.sleep) -> dict:
    """Block until the app answers. {"answer": {...}} | {"error": "app_not_reached" | "timed_out"}.
    The request files are removed either way."""
    rid = req["id"]
    start = clock()
    try:
        while True:
            st = _state(home, rid)
            ans = st["answer"]
            if ans.exists():
                try:
                    return {"answer": json.loads(ans.read_text())}
                except (OSError, ValueError):
                    return {"error": "bad_answer"}
            now = clock()
            if now >= req["expires_at"]:
                audit(home, f"expired {rid[:8]}")
                return {"error": "timed_out"}
            if not st["seen"] and now - start >= seen_timeout:
                audit(home, f"not_seen {rid[:8]}")
                return {"error": "app_not_reached"}
            sleep(poll)
    finally:
        cleanup(home, rid)


def open_answer(sk: X25519PrivateKey, req: dict, answer: dict) -> dict:
    """Decrypt the app's answer. ValueError when it doesn't open (wrong key, tampered,
    or bound to a different request)."""
    try:
        epk = b64d(answer["epk"])
        nonce = b64d(answer["nonce"])
        ct = b64d(answer["ct"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("malformed answer") from exc
    if len(epk) != 32 or len(nonce) != 12:
        raise ValueError("malformed answer")
    shared = sk.exchange(X25519PublicKey.from_public_bytes(epk))
    key = HKDF(hashes.SHA256(), 32, salt=epk + b64d(req["pk"]), info=_INFO).derive(shared)
    try:
        pt = AESGCM(key).decrypt(nonce, ct, transcript(req))
    except Exception as exc:  # noqa: BLE001 - InvalidTag
        raise ValueError("answer did not decrypt") from exc
    out = json.loads(pt.decode("utf-8"))
    if not isinstance(out, dict) or not isinstance(out.get("secret"), str):
        raise ValueError("malformed answer")
    return out


def seal(req: dict, payload: dict) -> dict:
    """What the app does (reference + tests): encrypt `payload` to this request."""
    pk = b64d(req["pk"])
    e = X25519PrivateKey.generate()
    epk = _pub_raw(e)
    shared = e.exchange(X25519PublicKey.from_public_bytes(pk))
    key = HKDF(hashes.SHA256(), 32, salt=epk + pk, info=_INFO).derive(shared)
    nonce = secrets.token_bytes(12)
    ct = AESGCM(key).encrypt(nonce, json.dumps(payload).encode(), transcript(req))
    return {"epk": b64e(epk), "nonce": b64e(nonce), "ct": b64e(ct)}


def verify(req: dict) -> bool:
    """Signature check (what the app does before showing the card)."""
    try:
        Ed25519PublicKey.from_public_bytes(b64d(req["bridge_key"])).verify(b64d(req["sig"]), transcript(req))
        return True
    except Exception:  # noqa: BLE001
        return False


# ── the door (answered by the tunnel; it never sees plain text) ─────────

def matches(path: str) -> bool:
    return _ROUTE.match((path or "").split("?", 1)[0]) is not None


def _err(status: int, code: str, message: str) -> tuple[int, dict]:
    return status, {"error": code, "message": message}


def pending(home: Path, *, now: float | None = None, mark_seen: bool = False) -> list[dict]:
    """Live requests, newest first; expired ones are removed."""
    now = now if now is not None else time.time()
    d = spool(home)
    out = []
    for p in d.glob("*.req.json"):
        rid = p.name.split(".", 1)[0]
        if not _ID.match(rid):
            continue
        try:
            req = json.loads(p.read_text())
        except (OSError, ValueError):
            continue
        if not isinstance(req, dict) or req.get("id") != rid:
            continue
        if now >= float(req.get("expires_at") or 0):
            cleanup(home, rid)
            continue
        if (d / f"{rid}.ans.json").exists():
            continue
        if mark_seen and not (d / f"{rid}.seen").exists():
            try:
                _write_private(d / f"{rid}.seen", b"", exclusive=True)
            except FileExistsError:
                pass
        out.append({k: req.get(k) for k in ("v", "id", "kind", "origin", "username", "label",
                                            "created_at", "expires_at", "pk", "bridge_key", "sig")})
    out.sort(key=lambda r: -int(r.get("created_at") or 0))
    return out


async def handle(method: str, path: str, headers: dict, body, *, hermes_url: str,
                 hermes_home: Path) -> tuple[int, dict]:
    from herald_relay import local_screen  # noqa: PLC0415

    m = _ROUTE.match((path or "").split("?", 1)[0])
    if m is None:
        return _err(404, "not_found", "Not found.")
    profile, rid = m.group("profile"), m.group("rid")
    method = (method or "GET").upper()
    if (rid and method != "POST") or (not rid and method != "GET"):
        return _err(405, "method_not_allowed", "Not allowed.")
    prefix = f"/p/{profile}" if profile else ""
    # Refuse before the body is even looked at.
    if not await local_screen._caller_owns(hermes_url.rstrip("/"), prefix, headers):
        return _err(401, "agent_key_required", "Send this agent's API key with the request.")
    home = local_screen._home(Path(hermes_home), profile)
    if home is None:
        return _err(404, "not_found", "We couldn't find that agent.")

    if not rid:
        return 200, {"requests": pending(home, mark_seen=True)}

    if isinstance(body, str):
        try:
            body = json.loads(body) if body else {}
        except ValueError:
            body = None
    if not isinstance(body, dict):
        return _err(422, "invalid_request", "Send the encrypted answer.")
    live = {r["id"]: r for r in pending(home)}
    if rid not in live:
        return _err(410, "gone", "That request has ended. Ask your agent to try again.")
    if body.get("decline") is True:
        answer = {"declined": True}
        event = "declined"
    else:
        ok = all(isinstance(body.get(k), str) and 0 < len(body[k]) <= MAX_CT_B64 for k in ("epk", "nonce", "ct"))
        if not ok:
            return _err(422, "invalid_request", "Send the encrypted answer.")
        try:
            if len(b64d(body["epk"])) != 32 or len(b64d(body["nonce"])) != 12:
                raise ValueError
        except ValueError:
            return _err(422, "invalid_request", "Send the encrypted answer.")
        answer = {"epk": body["epk"], "nonce": body["nonce"], "ct": body["ct"]}
        event = "answered"
    try:
        _write_private(spool(home) / f"{rid}.ans.json", json.dumps(answer).encode(), exclusive=True)
    except FileExistsError:
        return _err(409, "already_answered", "That request was already answered.")
    audit(home, f"{event} {rid[:8]} {live[rid]['kind']} {live[rid]['origin']}")
    return 200, {"ok": True, "id": rid, "status": event}
