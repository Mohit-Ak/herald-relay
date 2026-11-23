"""Secure entry (herald#398): the agent asks for one password, the app answers
with ciphertext, and only the waiting process can open it.

The phone's half is played by `secure_entry.seal` (the same construction the
Flutter app uses; flutter/test/secure_entry_test.dart pins the app against
vectors made here). One test stores a login in a REAL Hermes vault when Hermes
is installed on this machine.
"""
import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import is_local_door, local_skills, secure_entry, secure_entry_cli  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_local_screen import hermes  # noqa: E402,F401  (a tiny local Hermes that knows one key per profile)

SECRET = "KNOWN_SECRET_7f3a"


def door(hermes, home, method, path="/_herald/secure-entry", key="default-key", body=None):  # noqa: F811
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return secure_entry.handle(method, path, headers, body, hermes_url=hermes, hermes_home=home)


def every_file_under(home: Path):
    for p in home.rglob("*"):
        if p.is_file():
            yield p, p.read_bytes()


def test_only_our_paths_are_taken():
    rid = "a" * 32
    assert secure_entry.matches("/_herald/secure-entry")
    assert secure_entry.matches(f"/_herald/secure-entry/{rid}")
    assert secure_entry.matches(f"/p/work/_herald/secure-entry/{rid}")
    assert not secure_entry.matches("/_herald/secure-entry/../screen")
    assert not secure_entry.matches("/_herald/secure-entry/XYZ")
    assert not secure_entry.matches("/v1/_herald/secure-entry")
    assert is_local_door(f"/_herald/secure-entry/{rid}")      # both transports answer it locally


def test_origins_are_reduced_to_scheme_and_host():
    assert secure_entry.normalize_origin("https://Accounts.Google.com/signin?x=1") == "https://accounts.google.com"
    assert secure_entry.normalize_origin("example.com") == "https://example.com"
    assert secure_entry.normalize_origin("http://localhost:8080/a") == "http://localhost:8080"
    assert secure_entry.normalize_origin("https://a.com:443") == "https://a.com"
    for bad in ("", "javascript:alert(1)", "file:///etc/passwd", "ftp://x.com"):
        with pytest.raises(ValueError):
            secure_entry.normalize_origin(bad)


def test_a_request_is_signed_by_this_bridge_and_holds_no_private_key(tmp_path):
    req, sk = secure_entry.create_request(tmp_path, "password", "https://example.com/login", "ann@x.com")
    assert secure_entry.verify(req)
    assert req["origin"] == "https://example.com" and req["label"] == "example.com"
    assert req["expires_at"] - req["created_at"] == secure_entry.TTL_S["password"]
    # Same key every time for this profile; a different profile has its own.
    req2, _ = secure_entry.create_request(tmp_path, "password", "https://example.com")
    assert req2["bridge_key"] == req["bridge_key"]
    other, _ = secure_entry.create_request(tmp_path / "profiles/work", "password", "https://example.com")
    assert other["bridge_key"] != req["bridge_key"]
    # What a relay could change is caught by the signature.
    for field, value in (("origin", "https://evil.example"), ("pk", req2["pk"]), ("username", "eve"),
                         ("expires_at", req["expires_at"] + 9999)):
        assert not secure_entry.verify({**req, field: value})
    # Files are private, and nothing on disk can open an answer.
    d = secure_entry.spool(tmp_path)
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    f = d / f"{req['id']}.req.json"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    ident = tmp_path / "herald/bridge-identity.key"
    assert stat.S_IMODE(os.stat(ident).st_mode) == 0o600
    from cryptography.hazmat.primitives import serialization
    raw_sk = sk.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                              serialization.NoEncryption())
    for _, data in every_file_under(tmp_path):
        assert raw_sk not in data and secure_entry.b64e(raw_sk).encode() not in data


def test_an_answer_opens_only_with_its_own_request(tmp_path):
    req, sk = secure_entry.create_request(tmp_path, "password", "https://example.com", "ann")
    other, other_sk = secure_entry.create_request(tmp_path, "password", "https://example.com", "ann")
    sealed = secure_entry.seal(req, {"secret": SECRET, "username": "ann", "save": True})
    assert secure_entry.open_answer(sk, req, sealed)["secret"] == SECRET
    with pytest.raises(ValueError):
        secure_entry.open_answer(other_sk, other, sealed)           # wrong key
    with pytest.raises(ValueError):
        secure_entry.open_answer(sk, {**req, "origin": "https://evil.example"}, sealed)  # bound to the origin
    flipped = dict(sealed, ct=sealed["ct"][:-2] + ("A" if sealed["ct"][-2] != "A" else "B") + sealed["ct"][-1])
    with pytest.raises(ValueError):
        secure_entry.open_answer(sk, req, flipped)                  # tampered
    with pytest.raises(ValueError):
        secure_entry.open_answer(sk, req, {"epk": "x"})


@pytest.mark.parametrize("key", [None, "", "wrong", "work-key"])
async def test_no_key_or_another_profiles_key_gets_nothing(hermes, tmp_path, key):  # noqa: F811
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com")
    for method, path in (("GET", "/_herald/secure-entry"), ("POST", f"/_herald/secure-entry/{req['id']}")):
        status, body = await door(hermes, tmp_path, method, path, key=key, body={"decline": True})
        assert status == 401 and body["error"] == "agent_key_required"
    assert not (secure_entry.spool(tmp_path) / f"{req['id']}.seen").exists()
    assert not (secure_entry.spool(tmp_path) / f"{req['id']}.ans.json").exists()


async def test_the_whole_round_trip_carries_only_ciphertext(hermes, tmp_path, caplog):  # noqa: F811
    req, sk = secure_entry.create_request(tmp_path, "password", "https://example.com", "ann")
    status, body = await door(hermes, tmp_path, "GET")
    assert status == 200 and [r["id"] for r in body["requests"]] == [req["id"]]
    shown = body["requests"][0]
    assert secure_entry.verify(shown) and "sk" not in json.dumps(shown)
    assert (secure_entry.spool(tmp_path) / f"{req['id']}.seen").exists()   # the app picked it up

    sealed = secure_entry.seal(shown, {"secret": SECRET, "username": "ann", "save": False})
    status, body = await door(hermes, tmp_path, "POST", f"/_herald/secure-entry/{req['id']}", body=json.dumps(sealed))
    assert (status, body["status"]) == (200, "answered")
    # Answered: no longer offered, and a second answer is refused.
    assert (await door(hermes, tmp_path, "GET"))[1]["requests"] == []
    status, body = await door(hermes, tmp_path, "POST", f"/_herald/secure-entry/{req['id']}", body=sealed)
    assert status in (409, 410)

    got = secure_entry.wait_answer(tmp_path, req, poll=0.01)
    assert secure_entry.open_answer(sk, req, got["answer"]) == {"secret": SECRET, "username": "ann", "save": False}
    # Nothing left behind but the audit log, which names the site and never the value.
    left = sorted(p.name for p in secure_entry.spool(tmp_path).iterdir())
    assert left == ["audit.log"]
    log = (secure_entry.spool(tmp_path) / "audit.log").read_text()
    assert "request " in log and "answered " in log and "https://example.com" in log
    for _, data in every_file_under(tmp_path):
        assert SECRET.encode() not in data
    assert SECRET not in caplog.text


async def test_what_the_door_refuses(hermes, tmp_path):  # noqa: F811
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com")
    p = f"/_herald/secure-entry/{req['id']}"
    assert (await door(hermes, tmp_path, "POST", p, body={}))[0] == 422
    assert (await door(hermes, tmp_path, "POST", p, body="not json"))[0] == 422
    assert (await door(hermes, tmp_path, "POST", p, body={"epk": "AA", "nonce": "AA", "ct": "AA"}))[0] == 422
    assert (await door(hermes, tmp_path, "POST", p, body={"epk": "x" * 5000, "nonce": "a", "ct": "a"}))[0] == 422
    assert (await door(hermes, tmp_path, "POST", "/_herald/secure-entry/" + "b" * 32, body={"decline": True}))[0] == 410
    assert (await door(hermes, tmp_path, "GET", p))[0] == 405
    assert (await door(hermes, tmp_path, "POST"))[0] == 405
    assert (await door(hermes, tmp_path, "GET", "/p/nobody/_herald/secure-entry", key="nobody-key"))[0] in (401, 404)
    assert not (secure_entry.spool(tmp_path) / f"{req['id']}.ans.json").exists()


async def test_each_profile_sees_only_its_own_requests(hermes, tmp_path):  # noqa: F811
    (tmp_path / "profiles/work").mkdir(parents=True)
    mine, _ = secure_entry.create_request(tmp_path / "profiles/work", "password", "https://work.example")
    secure_entry.create_request(tmp_path, "password", "https://home.example")
    status, body = await door(hermes, tmp_path, "GET", "/p/work/_herald/secure-entry", key="work-key")
    assert status == 200 and [r["id"] for r in body["requests"]] == [mine["id"]]


async def test_decline_reaches_the_waiting_process(hermes, tmp_path):  # noqa: F811
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com")
    status, _ = await door(hermes, tmp_path, "POST", f"/_herald/secure-entry/{req['id']}", body={"decline": True})
    assert status == 200
    assert secure_entry.wait_answer(tmp_path, req, poll=0.01) == {"answer": {"declined": True}}


def test_expired_and_unseen_requests_end(tmp_path):
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com", now=time.time() - 3600)
    assert secure_entry.pending(tmp_path) == []
    assert not (secure_entry.spool(tmp_path) / f"{req['id']}.req.json").exists()

    clock = [1000.0]
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com", now=clock[0])
    def tick(_):
        clock[0] += 30
    got = secure_entry.wait_answer(tmp_path, req, seen_timeout=120, clock=lambda: clock[0], sleep=tick)
    assert got == {"error": "app_not_reached"}

    clock = [1000.0]
    req, _ = secure_entry.create_request(tmp_path, "password", "https://example.com", now=clock[0])
    secure_entry.pending(tmp_path, now=clock[0], mark_seen=True)
    got = secure_entry.wait_answer(tmp_path, req, seen_timeout=120, clock=lambda: clock[0], sleep=tick)
    assert got == {"error": "timed_out"}                                  # seen, but nobody typed
    assert [p.name for p in secure_entry.spool(tmp_path).iterdir()] == ["audit.log"]


# ── the agent's command ─────────────────────────────────────────────────

@pytest.fixture
def fake_store(tmp_path):
    """Stands in for secret_store.py run by Hermes' interpreter: records what came on stdin."""
    rec = tmp_path / "store.jsonl"
    script = tmp_path / "store.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, sys
        job = json.loads(sys.stdin.read())
        job["home"] = os.environ.get("HERMES_HOME")
        job["argv"] = sys.argv[1:]
        open({str(rec)!r}, "a").write(json.dumps(job) + "\\n")
        print(json.dumps({{"handle": "vault_abc123"}} if job["op"] == "add" else {{"removed": True}}))
    """))
    return [sys.executable, str(script)], rec


def answering(payload):
    """A wait_answer that plays the app: seals `payload` (or declines) for the request."""
    def wait(home, req, **_):
        secure_entry.cleanup(home, req["id"])
        if payload == "decline":
            return {"answer": {"declined": True}}
        if isinstance(payload, dict) and "error" in payload:
            return payload
        return {"answer": secure_entry.seal(req, payload)}
    return wait


def run_request(tmp_path, cmd, payload, monkeypatch, username="ann@x.com", kind="password"):
    spawned = []
    monkeypatch.setattr(secure_entry_cli, "_spawn_forget", lambda home, h, after: spawned.append((h, after)))
    out = secure_entry_cli.request(tmp_path, kind, "https://example.com/login", username, "", cmd,
                                   wait=answering(payload))
    return out, spawned


def test_a_typed_password_becomes_a_vault_handle(tmp_path, fake_store, monkeypatch, capsys):
    cmd, rec = fake_store
    out, spawned = run_request(tmp_path, cmd, {"secret": SECRET, "username": "ann@x.com", "save": True}, monkeypatch)
    assert out["ok"] and out["handle"] == "vault_abc123" and out["saved"] is True
    assert "browser_vault" in out["next"] and SECRET not in json.dumps(out)
    job = json.loads(rec.read_text().splitlines()[-1])
    assert job == {"op": "add", "origin": "https://example.com", "identifier": "ann@x.com", "password": SECRET,
                   "label": "example.com", "replace": True, "home": str(tmp_path), "argv": [str(secure_entry_cli._STORE)]}
    assert spawned == []
    assert SECRET not in capsys.readouterr().out
    log = (secure_entry.spool(tmp_path) / "audit.log").read_text()
    assert "stored " in log and "saved=yes" in log and SECRET not in log


def test_not_saving_removes_the_login_later(tmp_path, fake_store, monkeypatch):
    cmd, rec = fake_store
    out, spawned = run_request(tmp_path, cmd, {"secret": SECRET, "username": "bob", "save": False}, monkeypatch)
    assert out["ok"] and out["saved"] is False and out["identifier"] == "bob"
    assert json.loads(rec.read_text().splitlines()[-1])["replace"] is False   # an older saved login stays
    assert spawned == [("vault_abc123", secure_entry_cli.FORGET_AFTER_S)]
    # A forget that never ran (reboot) is done by the next request's sweep.
    assert secure_entry_cli.sweep(tmp_path, cmd, now=time.time() + 3600) == 1
    assert json.loads(rec.read_text().splitlines()[-1]) == {"op": "remove", "handle": "vault_abc123",
                                                           "home": str(tmp_path), "argv": [str(secure_entry_cli._STORE)]}
    assert secure_entry_cli._ephemeral(tmp_path) == {}


@pytest.mark.parametrize("payload,error", [
    ("decline", "declined"),
    ({"error": "app_not_reached"}, "app_not_reached"),
    ({"error": "timed_out"}, "timed_out"),
])
def test_no_answer_tells_the_agent_what_next(tmp_path, fake_store, monkeypatch, payload, error):
    out, _ = run_request(tmp_path, fake_store[0], payload, monkeypatch)
    assert not out["ok"] and out["error"] == error and out["next"]
    if error == "app_not_reached":
        assert "[[herald:connect:screen]]" in out["next"]      # falls back to takeover (#382)
    assert not fake_store[1].exists()


def test_codes_are_not_taken_yet_and_say_so(tmp_path, fake_store, monkeypatch):
    out, _ = run_request(tmp_path, fake_store[0], {"secret": "123456"}, monkeypatch, kind="code")
    assert out["error"] == "unsupported_kind" and "[[herald:connect:screen]]" in out["next"]
    assert secure_entry.pending(tmp_path) == []


def test_a_bad_origin_is_refused_before_anything_is_written(tmp_path, fake_store, monkeypatch):
    out = secure_entry_cli.request(tmp_path, "password", "javascript:alert(1)", "", "", fake_store[0],
                                   wait=answering({"secret": SECRET}))
    assert out["error"] == "invalid_request"
    assert not (tmp_path / "herald/secure-entry").exists() or secure_entry.pending(tmp_path) == []


def test_the_skill_tells_the_agent_the_exact_command(tmp_path):
    (tmp_path / "profiles/work").mkdir(parents=True)
    local_skills.install(tmp_path, command="/opt/h/bin/herald-tunnel", all_profiles=True)
    for home in (tmp_path, tmp_path / "profiles/work"):
        text = (home / "skills/herald/herald-passwords/SKILL.md").read_text()
        assert f'/opt/h/bin/herald-tunnel secret request --home "{home}" --kind password' in text
        assert "{{" not in text


# ── a real Hermes vault, when this machine has Hermes ───────────────────

def _hermes():
    from herald_relay import tunnel_cli
    return tunnel_cli._hermes_command()


HERMES_CMD = _hermes()   # read once at collection: other tests point HERMES_PYTHON at stand-ins


@pytest.mark.skipif(not HERMES_CMD, reason="Hermes is not installed here")
def test_the_real_vault_gets_the_login_bound_to_its_site(tmp_path):
    cmd = HERMES_CMD
    home = tmp_path / "home"
    home.mkdir()
    res = secure_entry_cli._store(home, {"op": "add", "origin": "https://example.com", "identifier": "ann",
                                         "password": SECRET, "label": "Example"}, cmd)
    assert res.get("handle", "").startswith("vault_") and res["origin"] == "https://example.com"
    again = secure_entry_cli._store(home, {"op": "add", "origin": "https://example.com", "identifier": "ann",
                                           "password": SECRET + "2", "label": "Example"}, cmd)
    assert again["replaced"] == 1                                  # newest password wins, one handle
    for _, data in every_file_under(home):                         # encrypted at rest
        assert SECRET.encode() not in data
    assert secure_entry_cli._store(home, {"op": "remove", "handle": again["handle"]}, cmd) == {"removed": True}


def test_the_shared_vector_still_opens():
    """The app's tests encrypt the same vector and must produce these exact bytes."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    v = json.loads((Path(__file__).with_name("secure_entry_vector.json")).read_text())
    req = v["request"]
    assert secure_entry.verify(req)
    assert secure_entry.b64e(secure_entry.transcript(req)) == v["transcript_b64"]
    sk = X25519PrivateKey.from_private_bytes(secure_entry.b64d(v["recipient_sk"]))
    assert secure_entry.open_answer(sk, req, v["answer"]) == json.loads(v["plaintext"])
