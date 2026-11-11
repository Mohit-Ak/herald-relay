"""Local agents: Herald's skills land in the user's Hermes, and the password
import runs against Hermes' real vault and prints counts only.
Contract: herald docs/internal/plans/screen-passwords-contract.md
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import local_skills, tunnel_cli  # noqa: E402

HERMES_VENV_PY = Path.home() / ".hermes/hermes-agent/venv/bin/python"
CSV = ("name,url,username,password,note\n"
       "gh,https://github.com/login,alice@x.com,pw-SECRET-1,\n"
       "bank,https://bank.example.com/in,alice,pw-SECRET-2,\n"
       "app,android://x@com.y/,a,b,\n")


def test_skills_install_into_the_hermes_home(tmp_path):
    written = local_skills.install(tmp_path)
    names = {p.parent.name for p in written}
    assert names == {"herald-screen", "herald-passwords"}
    screen = (tmp_path / "skills/herald/herald-screen/SKILL.md").read_text()
    pw = (tmp_path / "skills/herald/herald-passwords/SKILL.md").read_text()
    assert screen.startswith("---\nname: herald-screen\n")
    # the agent must ask first, and must know macOS/Windows can't
    assert "Never install or start a screen without the user's yes" in screen
    assert "macOS or Windows" in screen
    assert "browser_vault_fill" in pw and "herald-tunnel passwords import" in pw
    assert "--delete" in pw
    assert "{{" not in pw and "{{" not in screen
    # re-running overwrites, never duplicates
    local_skills.install(tmp_path)
    assert len(list((tmp_path / "skills/herald").iterdir())) == 2


def test_setup_installs_the_skills(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    (h / ".env").write_text("API_SERVER_KEY=k\n")
    (h / "config.yaml").write_text("herald:\n  device_token: tok\n  relay_url: https://r\n")
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", h)
    import argparse
    args = argparse.Namespace(relay_url="", label="x", new_token=False, no_systemd=True)
    assert tunnel_cli.cmd_setup(args) == 0
    assert (h / "skills/herald/herald-screen/SKILL.md").exists()
    assert (h / "skills/herald/herald-passwords/SKILL.md").exists()


def test_import_program_matches_the_cloud_copy():
    herald = Path.home() / "projects/herald-wt-sp/backend/cloud/herald_passwords_import.py"
    if not herald.exists():
        pytest.skip("herald checkout not here")
    mine = Path(tunnel_cli.__file__).with_name("passwords_import.py").read_text()
    assert mine == herald.read_text()


@pytest.mark.skipif(not HERMES_VENV_PY.exists(), reason="needs a real Hermes install")
def test_passwords_import_against_the_real_vault_prints_counts_only(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    f = tmp_path / "Chrome Passwords.csv"
    f.write_text(CSV)
    env = dict(os.environ, HERMES_PYTHON=str(HERMES_VENV_PY), HERMES_HOME=str(h))
    cli = [sys.executable, "-m", "herald_relay.tunnel_cli"]
    cwd = Path(tunnel_cli.__file__).resolve().parents[1]
    r = subprocess.run(cli + ["passwords", "import", "--file", str(f), "--profile", "Work",
                              "--delete"], capture_output=True, text=True, env=env, cwd=cwd)
    assert r.returncode == 0, r.stderr[-500:]
    out = json.loads(r.stdout.strip())
    assert out["added"] == 2 and out["skipped"] == 1 and out["deleted_file"] is True
    assert not f.exists()
    for s in ("pw-SECRET", "alice", "github", "bank"):
        assert s not in r.stdout and s not in r.stderr
    r = subprocess.run(cli + ["passwords", "count"], capture_output=True, text=True,
                       env=env, cwd=cwd)
    assert json.loads(r.stdout.strip())["logins"] == 2
    # really in Hermes' vault, bound to each site, password not readable as text
    lst = subprocess.run([str(HERMES_VENV_PY), "-m", "hermes_cli.main", "vault", "list"],
                         capture_output=True, text=True,
                         env=dict(env, COLUMNS="200"),
                         cwd=str(HERMES_VENV_PY.parents[2]))
    assert "github.com" in lst.stdout and "pw-SECRET" not in lst.stdout
    blob = (h / "vault" / "vault.json.enc").read_bytes()
    assert b"pw-SECRET" not in blob


@pytest.mark.skipif(not HERMES_VENV_PY.exists(), reason="needs a real Hermes install")
def test_delete_leaves_a_file_that_is_not_a_password_export_alone(tmp_path):
    """The agent runs this with a path the user said out loud. The wrong
    file must come back untouched, and the agent must be told."""
    h = tmp_path / ".hermes"
    h.mkdir()
    f = tmp_path / "taxes-2026.csv"
    f.write_text("date,amount\n2026-01-01,100\n")
    env = dict(os.environ, HERMES_PYTHON=str(HERMES_VENV_PY), HERMES_HOME=str(h))
    r = subprocess.run([sys.executable, "-m", "herald_relay.tunnel_cli", "passwords", "import",
                        "--file", str(f), "--delete"], capture_output=True, text=True, env=env,
                       cwd=Path(tunnel_cli.__file__).resolve().parents[1])
    assert r.returncode == 3
    assert json.loads(r.stdout.strip()) == {"error": "not_a_password_export"}
    assert f.read_text() == "date,amount\n2026-01-01,100\n"


def test_the_skill_names_the_command_this_install_really_has(tmp_path):
    """herald-tunnel lives in its own venv, which is not on the agent's PATH.
    Seen on a real box: the skill said `herald-tunnel ...`, and the agent
    would have got "command not found"."""
    local_skills.install(tmp_path, command="/opt/h/bin/herald-tunnel")
    pw = (tmp_path / "skills/herald/herald-passwords/SKILL.md").read_text()
    assert '/opt/h/bin/herald-tunnel passwords import --file "<path they gave>"' in pw
    assert "`/opt/h/bin/herald-tunnel passwords count`" in pw
    assert "\nherald-tunnel passwords" not in pw


def test_every_profile_gets_the_skills_and_a_bad_one_is_skipped(tmp_path, monkeypatch, capsys):
    h = tmp_path / ".hermes"
    for name in ("work", "home"):
        (h / "profiles" / name).mkdir(parents=True)
    (h / "profiles" / "notes.txt").write_text("not a profile")
    bad = h / "profiles" / "locked"
    bad.mkdir()
    (bad / "skills").write_text("a file where the folder should be")
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", h)
    assert tunnel_cli.cmd_skills(None) == 0
    for home in (h, h / "profiles/work", h / "profiles/home"):
        text = (home / "skills/herald/herald-passwords/SKILL.md").read_text()
        assert tunnel_cli._self_exec() + " passwords import" in text
    assert not (h / "profiles/notes.txt/skills").exists()
    assert "this Hermes and 2 profiles" in capsys.readouterr().out


def test_hermes_is_found_without_it_on_path(tmp_path, monkeypatch):
    """A systemd unit or a non-login shell has no ~/.local/bin on PATH."""
    home = tmp_path / "home"
    venv = home / ".hermes/hermes-agent/venv/bin"
    venv.mkdir(parents=True)
    py = venv / "python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    (home / ".local/bin").mkdir(parents=True)
    launcher = home / ".local/bin/hermes"
    launcher.write_text(f"#!{py}\n")
    launcher.chmod(0o755)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("HERMES_PYTHON", raising=False)
    assert tunnel_cli._hermes_python() == str(py)
    launcher.unlink()                       # no launcher at all: the venv itself
    assert tunnel_cli._hermes_python() == str(py)
    py.unlink()
    assert tunnel_cli._hermes_python() == ""
