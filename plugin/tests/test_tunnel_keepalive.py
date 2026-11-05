"""The tunnel must keep running on machines without systemd (containers, many
hosted agents). Found live: an agent started `herald-tunnel run` by hand, the
machine restarted, and the phone's link went dead.

Contract:
- `run` is single-instance (a lock), so a keepalive and a manual start never
  open two tunnels for one device;
- `ensure` starts a detached `run` only when none is running, and prints
  nothing (a Hermes no-agent cron job delivers stdout; empty = silent);
- `setup` on a machine without user systemd starts the tunnel now and adds a
  Hermes cron keepalive (once), instead of failing silently.
"""
import argparse
import io
import subprocess
import sys
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import tunnel_cli  # noqa: E402


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    (h / ".env").write_text("API_SERVER_KEY=k\n")
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", h)
    return h


def _holder(lock_path: Path) -> subprocess.Popen:
    """Another process holding the tunnel lock (a running tunnel)."""
    code = textwrap.dedent(f"""
        import fcntl, sys, time
        f = open({str(lock_path)!r}, "a+")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("held", flush=True)
        time.sleep(30)
    """)
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_run_refuses_a_second_tunnel(home, monkeypatch):
    (home / "config.yaml").write_text("herald:\n  relay_url: https://r.example\n  device_token: t\n")
    started = []
    monkeypatch.setattr(tunnel_cli, "HeraldRelayClient",
                        lambda **kw: started.append(kw) or (_ for _ in ()).throw(AssertionError("started")))
    lock = tunnel_cli._lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    p = _holder(lock)
    try:
        assert tunnel_cli.cmd_run(argparse.Namespace()) == 0
        assert started == []
    finally:
        p.kill()


def test_ensure_starts_a_tunnel_only_when_none_runs(home, monkeypatch):
    spawned = []
    monkeypatch.setattr(tunnel_cli, "_spawn_detached_run", lambda: spawned.append(1))
    out = io.StringIO()
    with redirect_stdout(out):
        assert tunnel_cli.cmd_ensure(argparse.Namespace()) == 0
    assert spawned == [1]
    assert out.getvalue() == ""

    lock = tunnel_cli._lock_path()
    p = _holder(lock)
    try:
        spawned.clear()
        with redirect_stdout(out):
            assert tunnel_cli.cmd_ensure(argparse.Namespace()) == 0
        assert spawned == []
        assert out.getvalue() == ""
    finally:
        p.kill()


def _setup_args(**kw):
    base = dict(relay_url="https://r.example", label="box", new_token=False, no_systemd=False)
    base.update(kw)
    return argparse.Namespace(**base)


def test_setup_without_systemd_adds_a_keepalive_and_starts_now(home, monkeypatch):
    monkeypatch.setattr(tunnel_cli, "_register_device", lambda u, l: "tok")
    monkeypatch.setattr(tunnel_cli, "_has_user_systemd", lambda: False)
    spawned, crons = [], []
    monkeypatch.setattr(tunnel_cli, "_spawn_detached_run", lambda: spawned.append(1))
    monkeypatch.setattr(tunnel_cli, "_hermes_cron", lambda argv: crons.append(argv) or (0, ""))
    with redirect_stdout(io.StringIO()):
        assert tunnel_cli.cmd_setup(_setup_args()) == 0
    assert spawned == [1]
    script = home / "scripts" / tunnel_cli.KEEPALIVE_SCRIPT
    assert script.is_file() and " ensure" in script.read_text()
    create = [a for a in crons if a[:1] == ["create"]]
    assert len(create) == 1
    a = create[0]
    assert "--no-agent" in a and "--script" in a and a[a.index("--script") + 1] == tunnel_cli.KEEPALIVE_SCRIPT  # Hermes wants the bare name
    assert a[a.index("--name") + 1] == tunnel_cli.KEEPALIVE_NAME
    assert not (Path.home() / ".config/systemd/user" / "never").exists()


def test_setup_adds_the_keepalive_only_once(home, monkeypatch):
    monkeypatch.setattr(tunnel_cli, "_register_device", lambda u, l: "tok")
    monkeypatch.setattr(tunnel_cli, "_has_user_systemd", lambda: False)
    monkeypatch.setattr(tunnel_cli, "_spawn_detached_run", lambda: None)
    crons = []

    def fake(argv):
        crons.append(argv)
        if argv[:1] == ["list"]:
            return 0, f"  abc123 [active]\n    Name: {tunnel_cli.KEEPALIVE_NAME}\n"
        return 0, ""
    monkeypatch.setattr(tunnel_cli, "_hermes_cron", fake)
    with redirect_stdout(io.StringIO()):
        assert tunnel_cli.cmd_setup(_setup_args()) == 0
    assert not [a for a in crons if a[:1] == ["create"]]


def test_setup_with_systemd_keeps_the_unit_path(home, monkeypatch, tmp_path):
    monkeypatch.setattr(tunnel_cli, "_register_device", lambda u, l: "tok")
    monkeypatch.setattr(tunnel_cli, "_has_user_systemd", lambda: True)
    monkeypatch.setattr(tunnel_cli.Path, "home", lambda: tmp_path)
    ran, crons = [], []
    monkeypatch.setattr(tunnel_cli.subprocess, "run", lambda cmd, check=False: ran.append(cmd))
    monkeypatch.setattr(tunnel_cli, "_hermes_cron", lambda argv: crons.append(argv) or (0, ""))
    monkeypatch.setattr(tunnel_cli, "_spawn_detached_run", lambda: crons.append("spawn"))
    with redirect_stdout(io.StringIO()):
        assert tunnel_cli.cmd_setup(_setup_args()) == 0
    assert (tmp_path / ".config/systemd/user" / tunnel_cli.UNIT_NAME).is_file()
    assert any("enable" in c for c in ran)
    assert crons == []
