"""The app adding a Chrome export to this computer's vault: who may, and what
is left behind. The import program is a stand-in here (it records what it was
given); one test runs the real one against a real Hermes vault when Hermes is
installed on this machine.
"""
import base64
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import local_passwords  # noqa: E402
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_local_screen import hermes  # noqa: E402,F401  (a tiny local Hermes that knows one key per profile)

CSV = "name,url,username,password,note\nExample,https://example.com/login,ann,KNOWN_PW_1,\n"


@pytest.fixture
def prog(tmp_path, monkeypatch):
    """Stands in for passwords_import.py: records the call and what the file held."""
    seen = tmp_path / "seen.jsonl"
    answer = tmp_path / "answer.json"
    answer.write_text(json.dumps({"added": 1, "updated": 0, "skipped": 0, "total": 1, "logins": 7}))
    script = tmp_path / "imp.py"
    script.write_text(textwrap.dedent(f"""
        import json, os, stat, sys
        a = sys.argv[1:]
        rec = {{"argv": a, "home": os.environ.get("HERMES_HOME")}}
        ans = json.loads(open({str(answer)!r}).read())
        if "--file" in a:
            p = a[a.index("--file") + 1]
            rec["path"] = p
            rec["mode"] = oct(stat.S_IMODE(os.stat(p).st_mode))
            rec["text"] = open(p).read()
            if ans.get("delete", True) and "error" not in ans:
                os.unlink(p)
        open({str(seen)!r}, "a").write(json.dumps(rec) + "\\n")
        ans.pop("delete", None)
        print(json.dumps(ans))
        sys.exit(ans.pop("exit", 3 if "error" in ans else 0))
    """))
    monkeypatch.setattr(local_passwords, "_PROG", script)
    local_passwords._imports.clear()
    local_passwords._busy.clear()
    return seen, answer


def calls(seen):
    return [json.loads(l) for l in seen.read_text().splitlines()] if seen.exists() else []


def call(hermes, tmp_path, method, path="/_herald/passwords", key="default-key", body=None):  # noqa: F811
    (tmp_path / "home/profiles/work").mkdir(parents=True, exist_ok=True)
    headers = {"Authorization": f"Bearer {key}"} if key is not None else {}
    return local_passwords.handle(method, path, headers, body, hermes_url=hermes,
                                  hermes_home=tmp_path / "home", hermes_python=sys.executable)


def upload(text=CSV, profile="Work Chrome"):
    return {"csv_b64": base64.b64encode(text.encode()).decode(), "profile": profile}


def test_only_our_paths_are_taken():
    assert local_passwords.matches("/_herald/passwords")
    assert local_passwords.matches("/p/work/_herald/passwords")
    assert not local_passwords.matches("/_herald/screen")
    assert not local_passwords.matches("/v1/_herald/passwords")
    assert not local_passwords.matches("/p/../_herald/passwords")


@pytest.mark.parametrize("key", [None, "", "wrong", "work-key"])
async def test_no_key_or_another_profiles_key_gets_nothing(hermes, tmp_path, prog, key):  # noqa: F811
    for method in ("GET", "POST"):
        status, body = await call(hermes, tmp_path, method, key=key, body=upload())
        assert status == 401 and body["error"] == "agent_key_required"
    assert calls(prog[0]) == []                       # the file was never written or read


async def test_count(hermes, tmp_path, prog):  # noqa: F811
    assert await call(hermes, tmp_path, "GET") == (200, {"logins": 7})
    assert calls(prog[0])[0]["argv"] == ["--count"]


async def test_import_gives_counts_only_and_leaves_no_file(hermes, tmp_path, prog):  # noqa: F811
    status, body = await call(hermes, tmp_path, "POST", body=upload())
    assert (status, body) == (200, {"added": 1, "updated": 0, "skipped": 0, "total": 1, "logins": 7})
    assert "KNOWN_PW_1" not in json.dumps(body)
    rec = calls(prog[0])[0]
    assert rec["text"] == CSV and rec["mode"] == "0o600"
    assert "--delete" in rec["argv"]
    assert base64.b64decode(rec["argv"][rec["argv"].index("--profile-b64") + 1]).decode() == "Work Chrome"
    assert rec["home"] == str(tmp_path / "home")
    assert not os.path.exists(rec["path"])


async def test_a_named_profile_imports_into_its_own_vault(hermes, tmp_path, prog):  # noqa: F811
    status, _ = await call(hermes, tmp_path, "POST", "/p/work/_herald/passwords", key="work-key", body=upload())
    assert status == 200
    assert calls(prog[0])[0]["home"] == str(tmp_path / "home/profiles/work")


async def test_the_edge_transport_sends_text(hermes, tmp_path, prog):  # noqa: F811
    status, _ = await call(hermes, tmp_path, "POST", body=json.dumps(upload()))
    assert status == 200


@pytest.mark.parametrize("answer,exit_code,status,code", [
    ({"error": "not_a_password_export"}, 3, 422, "not_a_password_export"),
    ({"error": "ValueError"}, 1, 502, "import_failed"),
    ({"error": "rm -rf / ; KNOWN_PW_1"}, 1, 502, "import_failed"),
    ({"nonsense": True}, 0, 502, "import_failed"),
])
async def test_a_failed_import_says_so_and_the_file_is_erased(hermes, tmp_path, prog, answer, exit_code, status, code, caplog):  # noqa: F811
    prog[1].write_text(json.dumps({**answer, "exit": exit_code, "delete": False}))
    got, body = await call(hermes, tmp_path, "POST", body=upload())
    assert (got, body["error"]) == (status, code)
    assert not os.path.exists(calls(prog[0])[0]["path"])
    assert "KNOWN_PW_1" not in json.dumps(body) and "KNOWN_PW_1" not in caplog.text


async def test_what_is_refused_before_anything_is_written(hermes, tmp_path, prog, monkeypatch):  # noqa: F811
    assert (await call(hermes, tmp_path, "POST", body={}))[0] == 422
    assert (await call(hermes, tmp_path, "POST", body={"csv_b64": "not base64 !"}))[0] == 422
    assert (await call(hermes, tmp_path, "POST", body={"csv_b64": ""}))[0] == 422
    big = "x" * (local_passwords.MAX_BYTES + 1)
    status, body = await call(hermes, tmp_path, "POST", body=upload(big))
    assert status == 413 and body["error"] == "too_large"
    assert (await call(hermes, tmp_path, "DELETE"))[0] == 405
    assert calls(prog[0]) == []
    monkeypatch.setattr(local_passwords, "IMPORTS_PER_10_MIN", 1)
    assert (await call(hermes, tmp_path, "POST", body=upload()))[0] == 200
    assert (await call(hermes, tmp_path, "POST", body=upload()))[0] == 429


HERMES_PY = Path.home() / ".hermes/hermes-agent/venv/bin/python"


@pytest.mark.skipif(not HERMES_PY.exists(), reason="Hermes is not installed here")
async def test_the_real_import_into_a_real_vault(hermes, tmp_path, monkeypatch):  # noqa: F811
    local_passwords._imports.clear()
    home = tmp_path / "home"
    home.mkdir()
    headers = {"Authorization": "Bearer default-key"}
    kw = dict(hermes_url=hermes, hermes_home=home, hermes_python=str(HERMES_PY))
    before = set(os.listdir(local_passwords._private_dir() or "/tmp"))
    status, body = await local_passwords.handle("POST", "/_herald/passwords", headers, upload(), **kw)
    assert status == 200 and body["added"] == 1 and body["logins"] == 1
    assert await local_passwords.handle("GET", "/_herald/passwords", headers, None, **kw) == (200, {"logins": 1})
    status, body = await local_passwords.handle("POST", "/_herald/passwords", headers, upload("hello,world\n1,2\n"), **kw)
    assert status == 422 and body["error"] == "not_a_password_export"
    left = [f for f in set(os.listdir(local_passwords._private_dir() or "/tmp")) - before if f.startswith("herald-pw-")]
    assert left == []
    # The vault on disk is encrypted: the password is nowhere in the profile's files.
    for p in home.rglob("*"):
        if p.is_file():
            assert b"KNOWN_PW_1" not in p.read_bytes()
