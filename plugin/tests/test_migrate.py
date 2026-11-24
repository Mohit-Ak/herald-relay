"""A Herald Bridge installed where it can't update itself (Hermes' venv,
pip --user, ~/herald-tunnel-venv) moves itself into ~/.herald-relay, keeping
the pairing, so legacy installs become self-updating with no manual step."""
import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import bridge_update as bu  # noqa: E402
from herald_relay import migrate, tunnel_cli  # noqa: E402

from tests.test_bridge_update import KEYS, NEW, manifest, sign  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("HERALD_BRIDGE_ROOT", "HERALD_BRIDGE_MIGRATE", "HERALD_BRIDGE_AUTO_UPDATE"):
        monkeypatch.delenv(k, raising=False)


def _layout(tmp_path) -> bu.Layout:
    return bu.Layout(root=tmp_path / ".herald-relay", versions=tmp_path / ".herald-relay-versions")


def _fake_stager(made: list):
    def stager(layout, m, wheel):
        t = layout.versions / m["version"]
        (t / "bin").mkdir(parents=True)
        (t / "bin" / "herald-tunnel").write_text("#!/bin/sh\n")
        made.append((m["version"], wheel))
        return t
    return stager


def _no_working(cmd):
    return subprocess.CompletedProcess(cmd, 1, "", "")


# ── may we move? ─────────────────────────────────────────────────────────────

def test_eligible_for_a_plain_install_in_hermes_venv(tmp_path):
    ok, why = migrate.eligibility({}, hermes_home=tmp_path / ".hermes", home=tmp_path,
                                  managed=False, source_checkout=False)
    assert (ok, why) == (True, "")


@pytest.mark.parametrize("kw,env,reason", [
    (dict(managed=True), {}, "already_managed"),
    (dict(cfg={"auto_update": False}), {}, "disabled"),
    ({}, {"HERALD_BRIDGE_AUTO_UPDATE": "0"}, "disabled"),
    ({}, {"HERALD_BRIDGE_MIGRATE": "0"}, "migrate_disabled"),
    (dict(source_checkout=True), {}, "source_checkout"),
    (dict(hermes_home="OTHER"), {}, "custom_hermes_home"),
])
def test_not_moved_and_says_why(tmp_path, monkeypatch, kw, env, reason):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    args = dict(cfg={}, hermes_home=tmp_path / ".hermes", home=tmp_path,
                managed=False, source_checkout=False)
    if kw.get("hermes_home") == "OTHER":
        kw["hermes_home"] = tmp_path / "herald-review"
    args.update(kw)
    cfg = args.pop("cfg")
    assert migrate.eligibility(cfg, **args) == (False, reason)


def test_editable_dev_install_counts_as_source_checkout():
    # The test venv installs the plugin with `pip install -e plugin`.
    assert migrate.is_source_checkout() is True


def test_installed_under_new_name_is_not_a_bare_checkout(monkeypatch):
    # After the rename to herald-bridge, an installed wheel must still be seen
    # as an install (and moved), not as a bare checkout.
    import importlib.metadata as md

    import herald_relay
    site = Path(herald_relay.__file__).resolve().parents[1]

    class Dist:  # a regular (non-editable) wheel install under the new name
        def read_text(self, _name):
            return "{}"

        def locate_file(self, _p):
            return site
    seen = []

    def fake(name):
        seen.append(name)
        if name == "herald-bridge":
            return Dist()
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(md, "distribution", fake)
    assert migrate.is_source_checkout() is False
    assert seen == ["hermes-herald", "herald-bridge"]


# ── install the owned copy ───────────────────────────────────────────────────

def test_installs_the_signed_release_and_links_root(tmp_path):
    lay, made, fetched = _layout(tmp_path), [], []

    async def fetch():
        fetched.append(1)
        return sign(manifest())

    async def download(m):
        return tmp_path / m["wheel_name"]

    v = asyncio.run(migrate.prepare(lay, fetch=fetch, download=download,
                                    stager=_fake_stager(made), runner=_no_working, keys=KEYS))
    assert v == NEW and fetched == [1] and made[0][0] == NEW
    assert lay.root.is_symlink()
    assert Path(lay.root.resolve()) == (lay.versions / NEW).resolve()
    # The shape the updater expects: <root>-versions/<v>, root a symlink.
    assert bu.Layout.detect(str(lay.versions / NEW), home=tmp_path, in_venv=True) == lay


def test_reuses_a_working_herald_relay(tmp_path):
    lay = _layout(tmp_path)
    (lay.root / "bin").mkdir(parents=True)
    exe = lay.root / "bin" / "herald-tunnel"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)

    def runner(cmd):
        return subprocess.CompletedProcess(cmd, 0, json.dumps({"version": "0.8.0"}) + "\n", "")

    async def boom():
        raise AssertionError("must not fetch")

    v = asyncio.run(migrate.prepare(lay, fetch=boom, download=boom, runner=runner, keys=KEYS))
    assert v == "0.8.0" and not lay.root.is_symlink()


def test_a_tampered_manifest_installs_nothing(tmp_path):
    lay = _layout(tmp_path)
    env = sign(manifest())
    env["signatures"][0]["sig"] = env["signatures"][0]["sig"][::-1]

    async def fetch():
        return env

    async def download(m):
        raise AssertionError("must not download")

    with pytest.raises(bu.UpdateError) as e:
        asyncio.run(migrate.prepare(lay, fetch=fetch, download=download,
                                    stager=_fake_stager([]), runner=_no_working, keys=KEYS))
    assert e.value.code == "bad_signature"
    assert not lay.root.exists() and not lay.root.is_symlink()


def test_a_broken_real_dir_is_moved_aside_not_deleted(tmp_path):
    lay = _layout(tmp_path)
    lay.root.mkdir()
    (lay.root / "keep.txt").write_text("mine")

    async def fetch():
        return sign(manifest())

    async def download(m):
        return tmp_path / "w.whl"

    asyncio.run(migrate.prepare(lay, fetch=fetch, download=download,
                                stager=_fake_stager([]), runner=_no_working, keys=KEYS))
    aside = [p for p in tmp_path.iterdir() if p.name.startswith(".herald-relay.broken-")]
    assert len(aside) == 1 and (aside[0] / "keep.txt").read_text() == "mine"
    assert lay.root.is_symlink()


# ── point the launchers at it ────────────────────────────────────────────────

UNIT = """[Unit]
Description=Herald relay tunnel

[Service]
Type=simple
Environment=HERMES_HOME=/home/u/.hermes
Environment=HERALD_LOCAL_HERMES_URL=http://127.0.0.1:8642
ExecStart={exec}
Restart=always
"""


@pytest.mark.parametrize("old", [
    "/home/u/herald-tunnel-venv/bin/herald-tunnel run",
    "/home/u/.hermes/hermes-agent/venv/bin/herald-tunnel run",
    "/home/u/.local/bin/herald-tunnel run",
    "/usr/bin/python3 -m herald_relay.tunnel_cli run",
])
def test_unit_execstart_moves_and_everything_else_stays(tmp_path, old):
    root = tmp_path / ".herald-relay"
    f = tmp_path / "herald-tunnel.service"
    f.write_text(UNIT.format(exec=old))
    assert migrate.repoint_unit(f, str(root / "bin" / "herald-tunnel"), root) is True
    assert f.read_text() == UNIT.format(exec=f"{root}/bin/herald-tunnel run")


@pytest.mark.parametrize("line", [
    "{root}/bin/herald-tunnel run",      # already owned
    "/usr/bin/some-other-daemon --flag",  # not ours
])
def test_unit_left_alone_when_not_a_movable_tunnel(tmp_path, line):
    root = tmp_path / ".herald-relay"
    f = tmp_path / "x.service"
    f.write_text(UNIT.format(exec=line.format(root=root)))
    before = f.read_text()
    assert migrate.repoint_unit(f, str(root / "bin" / "herald-tunnel"), root) is False
    assert f.read_text() == before


def test_current_unit_from_cgroup_v2():
    cg = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/herald-tunnel.service\n"
    assert migrate.current_unit(cg) == "herald-tunnel.service"
    assert migrate.current_unit("0::/user.slice/user-1000.slice/session-3.scope\n") is None


def test_repoint_launchers_unit_keepalive_and_skills(tmp_path):
    root = tmp_path / ".herald-relay"
    hh = tmp_path / ".hermes"
    (hh / "scripts").mkdir(parents=True)
    ka = hh / "scripts" / "herald_tunnel_keepalive.sh"
    ka.write_text("#!/bin/sh\n# Herald: restart the relay tunnel if it is not running.\n"
                  "exec /home/u/herald-tunnel-venv/bin/herald-tunnel ensure\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "herald-tunnel.service").write_text(
        UNIT.format(exec="/home/u/herald-tunnel-venv/bin/herald-tunnel run"))
    # A different tunnel's unit (another HERMES_HOME) is never touched.
    other = UNIT.format(exec="/home/u/herald-tunnel-venv/bin/herald-tunnel run")
    (units / "herald-review-tunnel.service").write_text(other)

    changed = migrate.repoint_launchers(root, hermes_home=hh, unit_dir=units,
                                        unit="herald-tunnel.service", reload=False)
    new_bin = f"{root}/bin/herald-tunnel"
    assert changed == ["unit:herald-tunnel.service", "keepalive", "skills"]
    assert f"ExecStart={new_bin} run" in (units / "herald-tunnel.service").read_text()
    assert (units / "herald-review-tunnel.service").read_text() == other
    assert ka.read_text().endswith(f"exec {new_bin} ensure\n")
    skills = [p.read_text() for p in (hh / "skills" / "herald").rglob("SKILL.md")]
    assert any(new_bin in s for s in skills)


# ── the running tunnel moves itself ──────────────────────────────────────────

def _updater(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(bu, "IDLE_FOR_S", 0.0)
    monkeypatch.setattr(bu, "IDLE_POLL_S", 0.01)
    hh = tmp_path / ".hermes"
    calls = {"reexec": [], "repoint": []}

    async def fetch():
        return sign(manifest())

    def repoint(root, *, hermes_home, unit=None):
        calls["repoint"].append((root, hermes_home))
        return ["unit:herald-tunnel.service"]

    up = bu.BridgeUpdater(
        relay_url="https://relay.test", device_token="tok", busy=lambda: False,
        layout=None, detect=False, migration=True, move_to=_layout(tmp_path),
        hermes_home=hh, state_file=hh / "herald" / "bridge-update.json",
        fetch=fetch, stager=_fake_stager([]), keys=KEYS,
        reexec=lambda root, argv: calls["reexec"].append((root, argv)),
        repoint=repoint, **kw)
    return up, calls, hh


def test_running_tunnel_moves_then_reexecs_from_owned_root(tmp_path, monkeypatch):
    async def download(m):
        return tmp_path / m["wheel_name"]

    monkeypatch.setattr(bu, "download_wheel", lambda layout, m: download(m))
    up, calls, hh = _updater(tmp_path, monkeypatch)
    st = up.status()
    assert st["auto_update"] is True and st["managed"] is False and st["move"] == "pending"

    assert asyncio.run(up.migrate_now()) == "moved"
    root = tmp_path / ".herald-relay"
    assert calls["reexec"] == [(root, ["run"])]
    assert calls["repoint"] == [(root, hh)]
    rec = json.loads((hh / "herald" / "bridge-update.json").read_text())["migration"]
    assert rec["to"] == str(root) and rec["version"] == NEW and rec["result"] == "moved"
    # The new copy is asked to check for a newer release straight away.
    assert (hh / "herald" / "bridge-update.request").exists()


def test_move_waits_until_nothing_is_in_flight(tmp_path, monkeypatch):
    async def download(m):
        return tmp_path / m["wheel_name"]

    monkeypatch.setattr(bu, "download_wheel", lambda layout, m: download(m))
    busy = {"n": 5}

    def is_busy():
        busy["n"] -= 1
        return busy["n"] > 0

    up, calls, _ = _updater(tmp_path, monkeypatch)
    up.busy = is_busy
    asyncio.run(up.migrate_now())
    assert busy["n"] <= 0 and len(calls["reexec"]) == 1


def test_a_failed_move_reports_and_keeps_running(tmp_path, monkeypatch):
    async def download(m):
        raise bu.UpdateError("download_http", "502")

    monkeypatch.setattr(bu, "download_wheel", lambda layout, m: download(m))
    up, calls, _ = _updater(tmp_path, monkeypatch)
    assert asyncio.run(up.migrate_now()) == "download_http"
    assert calls["reexec"] == [] and up.status()["error"] == "move:download_http"


def test_not_eligible_stays_not_managed(tmp_path):
    up = bu.BridgeUpdater(relay_url="https://r", device_token="t", busy=lambda: False,
                          layout=None, detect=False, migration=False,
                          migration_reason="source_checkout")
    st = up.status()
    assert st["auto_update"] is False and st["reason"] == "not_managed"
    assert st["move"] == "source_checkout"


# ── setup from a non-owned venv re-runs itself from ~/.herald-relay ──────────

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    h = tmp_path / ".hermes"
    h.mkdir()
    (h / ".env").write_text("API_SERVER_KEY=k\n")
    (h / "config.yaml").write_text("herald:\n  device_token: keep-me\n  relay_url: https://r.test\n")
    monkeypatch.setattr(tunnel_cli, "HERMES_HOME", h)
    return h


def _args(**kw):
    a = dict(relay_url="", label="box", new_token=False, no_systemd=True)
    a.update(kw)
    return argparse.Namespace(**a)


def test_setup_moves_first_and_reruns_from_owned_copy(tmp_path, hermes_home, monkeypatch):
    seen = {}

    async def fake_prepare(layout, *, fetch, download, **_):
        seen["root"] = layout.root
        return "9.9.9"

    monkeypatch.setattr(migrate, "eligibility", lambda *a, **k: (True, ""))
    monkeypatch.setattr(migrate, "target_layout", lambda: _layout(tmp_path))
    monkeypatch.setattr(migrate, "prepare", fake_prepare)
    monkeypatch.setattr(sys, "argv", ["herald-tunnel", "setup", "--no-systemd"])
    reexec = []
    assert tunnel_cli._move_before_setup(_args(), reexec=lambda r, a: reexec.append((r, a)))
    assert reexec == [(tmp_path / ".herald-relay", ["setup", "--no-systemd"])]
    # The pairing is untouched: it lives in config.yaml, not in the venv.
    assert "keep-me" in (hermes_home / "config.yaml").read_text()
    rec = json.loads((hermes_home / "herald" / "bridge-update.json").read_text())["migration"]
    assert rec["version"] == "9.9.9"


def test_setup_carries_on_in_place_when_the_move_fails(tmp_path, hermes_home, monkeypatch, capsys):
    async def failing(layout, **_):
        raise bu.UpdateError("no_release")

    monkeypatch.setattr(migrate, "eligibility", lambda *a, **k: (True, ""))
    monkeypatch.setattr(migrate, "target_layout", lambda: _layout(tmp_path))
    monkeypatch.setattr(migrate, "prepare", failing)
    assert tunnel_cli._move_before_setup(_args(), reexec=lambda r, a: pytest.fail("no reexec")) is False
    assert "no_release" in capsys.readouterr().err
    # ...and the rest of setup still runs and keeps the token.
    monkeypatch.setattr(tunnel_cli, "_install_skills", lambda: None)
    assert tunnel_cli.cmd_setup(_args()) == 0
    assert "keep-me" in (hermes_home / "config.yaml").read_text()
