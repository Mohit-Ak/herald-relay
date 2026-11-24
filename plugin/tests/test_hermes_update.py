"""Herald Bridge keeps Hermes updated on a Herald-tested release: signed pin,
carried-patch guard, never past the tested release or backwards, staged
compile, idle gate, install via `hermes update`, restart, health through the
relay, rollback. The git checks run against real repositories."""
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import hermes_update as hu  # noqa: E402

from tests.test_bridge_update import KEYS, manifest, sign  # noqa: E402


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("HERALD_HERMES_AUTO_UPDATE", raising=False)
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@t", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(hu, "IDLE_FOR_S", 0.0)
    monkeypatch.setattr(hu, "HEALTH_WAIT_S", 5.0)


@pytest.fixture
def repos(tmp_path):
    """upstream (v0.1.0, v0.2.0) and a checkout on v0.1.0, like a stable install."""
    src = tmp_path / "src"
    src.mkdir()
    git(src, "init", "-q", "-b", "main")
    (src / "gateway").mkdir()
    (src / "hermes_cli").mkdir()
    (src / "gateway" / "run.py").write_text("VERSION = '0.1.0'\n")
    (src / "hermes_cli" / "main.py").write_text("print('hi')\n")
    git(src, "add", "-A")
    git(src, "commit", "-qm", "one")
    git(src, "tag", "-a", "v0.1.0", "-m", "v0.1.0")  # annotated, like upstream
    (src / "gateway" / "run.py").write_text("VERSION = '0.2.0'\n")
    git(src, "commit", "-qam", "two")
    git(src, "tag", "-a", "v0.2.0", "-m", "v0.2.0")
    up = tmp_path / "up.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(up)], check=True)
    co = tmp_path / "hermes-agent"
    subprocess.run(["git", "clone", "-q", str(up), str(co)], check=True)
    git(co, "checkout", "-q", "--detach", "v0.1.0")
    shas = {t: git(src, "rev-parse", f"{t}^{{commit}}") for t in ("v0.1.0", "v0.2.0")}
    return {"src": src, "up": up, "co": co, "sha": shas}


def pin_for(repos, tag="v0.2.0", pct=100, sha=None):
    return {"tag": tag, "version": tag[1:], "git_sha": sha or repos["sha"][tag],
            "rollout_percent": pct}


def box_for(repos, run=hu._run, **kw):
    return hu.Box(root=repos["co"], hermes=kw.pop("hermes", "hermes"),
                  python=kw.pop("python", sys.executable), units=["hermes-gateway.service"],
                  run=run, **kw)


# ── the signed pin ───────────────────────────────────────────────────────────

def test_pin_validates_and_defaults_to_nobody():
    good = {"tag": "v0.23.3", "version": "0.23.3", "git_sha": "a" * 40}
    assert hu.pin_from({"hermes": good})["rollout_percent"] == 0
    assert hu.pin_from({}) is None


@pytest.mark.parametrize("bad", [
    {"tag": "v0.23.3+canary.1", "version": "0.23.3", "git_sha": "a" * 40},  # not stable
    {"tag": "v0.23.3", "version": "0.23.4", "git_sha": "a" * 40},           # mismatch
    {"tag": "v0.23.3", "version": "0.23.3", "git_sha": "main"},             # not a commit
    {"tag": "v0.23.3", "version": "0.23.3", "git_sha": "a" * 40, "rollout_percent": 101},
    "v0.23.3",
])
def test_malformed_pin_is_ignored_not_fatal(bad):
    assert hu.pin_from({"hermes": bad}) is None


def test_opt_out(monkeypatch):
    assert hu.opted_out({"hermes_auto_update": False})
    monkeypatch.setenv("HERALD_HERMES_AUTO_UPDATE", "0")
    assert hu.opted_out({})


# ── preflight: never clobber, never past what we tested, never backwards ─────

def test_preflight_ok_on_a_clean_stable_checkout(repos):
    facts = hu.preflight(box_for(repos), pin_for(repos))
    assert facts == {"from_sha": repos["sha"]["v0.1.0"], "from_ref": None}


def test_uncommitted_change_is_skipped_and_reported(repos):
    (repos["co"] / "gateway" / "run.py").write_text("PATCHED = 1\n")
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos))
    assert e.value.code == "local_changes"


def test_carried_patch_commit_is_skipped_and_reported(repos):
    co = repos["co"]
    git(co, "checkout", "-q", "-b", "local-patches")
    (co / "gateway" / "soul.py").write_text("def soul(): ...\n")
    git(co, "add", "-A")
    git(co, "commit", "-qm", "carry: /api/soul")
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos))
    assert e.value.code == "carried_patches" and "1 local commit" in e.value.detail


def test_already_on_the_tested_release(repos):
    git(repos["co"], "checkout", "-q", "--detach", "v0.2.0")
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos))
    assert e.value.code == "current"


def test_tag_must_point_at_the_signed_commit(repos):
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos, sha="b" * 40))
    assert e.value.code == "tag_mismatch"


def test_never_downgrades(repos):
    git(repos["co"], "checkout", "-q", "--detach", "v0.2.0")
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos, tag="v0.1.0"))
    assert e.value.code == "ahead_or_diverged"


def test_waits_when_upstream_published_a_newer_untested_release(repos):
    src = repos["src"]
    (src / "gateway" / "run.py").write_text("VERSION = '0.3.0'\n")
    git(src, "commit", "-qam", "three")
    git(src, "tag", "-a", "v0.3.0", "-m", "v0.3.0")
    git(src, "tag", "v0.3.0+canary.1")  # pre-releases don't count
    git(src, "push", "-q", str(repos["up"]), "main", "--tags")
    with pytest.raises(hu.Skip) as e:
        hu.preflight(box_for(repos), pin_for(repos))
    assert e.value.code == "upstream_ahead_of_tested"


def test_stage_compiles_the_release_in_a_throwaway_worktree(repos, tmp_path):
    box = box_for(repos)
    hu.stage(box, pin_for(repos), tmp_path / "stage")
    assert git(repos["co"], "rev-parse", "HEAD") == repos["sha"]["v0.1.0"]  # live untouched
    assert len(git(repos["co"], "worktree", "list").splitlines()) == 1     # cleaned up


def test_stage_refuses_a_release_that_does_not_compile(repos, tmp_path):
    src = repos["src"]
    (src / "gateway" / "run.py").write_text("def broken(:\n")
    git(src, "commit", "-qam", "broken")
    git(src, "tag", "-a", "v0.2.1", "-m", "v0.2.1")
    git(src, "push", "-q", str(repos["up"]), "main", "--tags")
    git(repos["co"], "fetch", "-q", "origin", "--tags")
    sha = git(src, "rev-parse", "v0.2.1^{commit}")
    with pytest.raises(hu.Skip) as e:
        hu.stage(box_for(repos), pin_for(repos, tag="v0.2.1", sha=sha), tmp_path / "stage")
    assert e.value.code == "stage_failed"


def test_detect_refuses_a_fork_or_a_box_without_a_gateway_unit(repos):
    def runner(cmd, **kw):
        if cmd[:2] == ["systemctl", "--user"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return hu._run(cmd, **kw)

    with pytest.raises(hu.Skip) as e:
        hu.detect_box(run=runner, root=repos["co"], hermes="hermes", python=sys.executable)
    assert e.value.code == "unofficial_origin"
    git(repos["co"], "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent.git")
    with pytest.raises(hu.Skip) as e:
        hu.detect_box(run=runner, root=repos["co"], hermes="hermes", python=sys.executable)
    assert e.value.code == "no_gateway_unit"


def test_detect_finds_active_gateway_units(repos):
    git(repos["co"], "remote", "set-url", "origin", "https://github.com/NousResearch/hermes-agent")

    def runner(cmd, **kw):
        if cmd[:2] == ["systemctl", "--user"]:
            return subprocess.CompletedProcess(cmd, 0, (
                "hermes-gateway.service loaded active running Hermes gateway\n"
                "hermes-gateway-work.service loaded inactive dead Hermes gateway (work)\n"), "")
        return hu._run(cmd, **kw)

    box = hu.detect_box(run=runner, root=repos["co"], hermes="hermes", python=sys.executable)
    assert box.units == ["hermes-gateway.service"] and box.remote == "origin"


# ── the whole update, against a real checkout ────────────────────────────────

class Fleet:
    """A fake systemd + Hermes HTTP for one box: /health reports the version of
    whatever the checkout has, as a restarted gateway would."""

    def __init__(self, repos, *, broken_on=None, broken_check="/api/soul", busy=False):
        self.repos = repos
        self.restarts = []
        self.broken_on = broken_on        # version whose health fails
        self.broken_check = broken_check
        self.busy = busy
        self.running = "0.1.0"            # version the live gateway serves

    def run(self, cmd, **kw):
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            self.restarts.append(cmd[3])
            self.running = git(self.repos["co"], "show", "HEAD:gateway/run.py").split("'")[1]
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return hu._run(cmd, **kw)

    async def get(self, url, headers):
        if url.endswith("/health/detailed"):
            return 200, {"gateway_busy": self.busy, "readiness": {"checks": {
                "background_queues": {"active_api_runs": 0, "active_delegations": 0}}}}
        if url.endswith("/health") and "/hermes/d/" not in url:
            return 200, {"status": "ok", "version": self.running}
        if self.running == self.broken_on and url.endswith(self.broken_check):
            return 500, {"error": "boom"}
        return 200, {}


def fake_hermes(tmp_path, repos, *, exit_code=0, to="v0.2.0") -> str:
    """Stands in for `hermes update`: moves the checkout to the release."""
    p = tmp_path / "hermes"
    p.write_text(f"#!/bin/sh\n[ \"$1\" = update ] || exit 2\n"
                 f"git -C {repos['co']} checkout -q --detach {to}\nexit {exit_code}\n")
    p.chmod(0o755)
    return str(p)


def updater(tmp_path, repos, fleet, *, hermes, pct=100, **kw):
    pin = pin_for(repos, pct=pct)

    async def fetch():
        return sign(manifest(hermes=pin))

    async def nosleep(_):
        await asyncio.sleep(0)

    box = box_for(repos, run=fleet.run, hermes=hermes)
    return hu.HermesUpdater(
        relay_url="https://relay.test", device_token="tok", api_key="k",
        hermes_home=tmp_path / ".hermes", fetch=fetch, keys=KEYS,
        detect=lambda: box, http_get=fleet.get, sleep=nosleep, **kw)


def head(repos):
    return git(repos["co"], "rev-parse", "HEAD")


def test_updates_restarts_and_checks_health(tmp_path, repos):
    fleet = Fleet(repos)
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos))
    assert asyncio.run(up.check_and_apply()) == "updated"
    assert head(repos) == repos["sha"]["v0.2.0"]
    assert fleet.restarts == ["hermes-gateway.service"] and fleet.running == "0.2.0"
    st = json.loads(up.state_file.read_text())
    assert st["last_good"]["version"] == "0.2.0" and "pending" not in st
    s = up.status()
    assert s["state"] == "current" and s["current"] == "0.2.0" and s["enabled"] is True


def test_a_gateway_still_serving_the_old_version_counts_as_failed(tmp_path, repos):
    # The restart "succeeded" but the process answering is still the old one
    # (wrong unit, a stale venv...): /health alone would look fine.
    fleet = Fleet(repos)
    real_run = fleet.run

    def restart_does_nothing(cmd, **kw):
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            fleet.restarts.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)

    fleet.run = restart_does_nothing
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos))
    assert asyncio.run(up.check_and_apply()) == "rolled_back"
    assert json.loads(up.state_file.read_text())["bad"]["0.2.0"] == "health:version_0.1.0"
    assert head(repos) == repos["sha"]["v0.1.0"]


def test_failed_health_through_the_relay_rolls_back(tmp_path, repos):
    fleet = Fleet(repos, broken_on="0.2.0", broken_check="/api/soul")
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos))
    assert asyncio.run(up.check_and_apply()) == "rolled_back"
    assert head(repos) == repos["sha"]["v0.1.0"] and fleet.running == "0.1.0"
    assert fleet.restarts == ["hermes-gateway.service"] * 2
    st = json.loads(up.state_file.read_text())
    assert st["bad"] == {"0.2.0": "health:relay_soul"} and "halted" not in st
    # A known-bad version isn't tried again (unless ops force it).
    assert asyncio.run(up.check_and_apply()) == "known_bad"


def test_hermes_update_failing_rolls_back(tmp_path, repos):
    fleet = Fleet(repos)
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos, exit_code=1))
    assert asyncio.run(up.check_and_apply()) == "rolled_back"
    assert head(repos) == repos["sha"]["v0.1.0"]
    assert "update_exit_1" in json.loads(up.state_file.read_text())["bad"]["0.2.0"]


def test_landing_on_an_untested_commit_rolls_back(tmp_path, repos):
    fleet = Fleet(repos)
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos, to="v0.1.0^0"))
    # (stands in for `hermes update` resolving the channel somewhere else)
    git(repos["co"], "checkout", "-q", "--detach", "v0.1.0")
    assert asyncio.run(up.check_and_apply()) == "rolled_back"
    assert json.loads(up.state_file.read_text())["bad"]["0.2.0"] == "unexpected_target"


def test_a_failed_rollback_halts_until_an_operator_looks(tmp_path, repos):
    fleet = Fleet(repos, broken_on="0.2.0")
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos))
    fleet_get = fleet.get

    async def everything_broken(url, headers):
        code, body = await fleet_get(url, headers)
        if "/hermes/d/" in url and url.endswith("/api/soul"):
            return 500, {}
        return code, body

    up._get = everything_broken
    assert asyncio.run(up.check_and_apply()) == "rollback_failed"
    st = json.loads(up.state_file.read_text())
    assert st["halted"].startswith("rollback_failed")
    assert asyncio.run(up.check_and_apply(force=True)) == "halted"


def test_waits_for_an_idle_gateway(tmp_path, repos, monkeypatch):
    fleet = Fleet(repos, busy=True)
    up = updater(tmp_path, repos, fleet, hermes=fake_hermes(tmp_path, repos))
    monkeypatch.setattr(hu, "IDLE_GIVE_UP_S", 0.0)
    assert asyncio.run(up.check_and_apply()) == "never_idle"
    assert head(repos) == repos["sha"]["v0.1.0"] and fleet.restarts == []


def test_gateway_idle_falls_back_to_the_status_file(tmp_path, repos):
    async def old_hermes(url, headers):
        return 404, None

    up = updater(tmp_path, repos, Fleet(repos), hermes="hermes")
    up._get = old_hermes
    (tmp_path / ".hermes").mkdir()
    (tmp_path / ".hermes" / "gateway_state.json").write_text(json.dumps({"active_agents": 2}))
    assert asyncio.run(up.gateway_idle()) is False
    (tmp_path / ".hermes" / "gateway_state.json").write_text(json.dumps({"active_agents": 0}))
    assert asyncio.run(up.gateway_idle()) is True
    up.busy = lambda: True  # the bridge itself has a phone request in flight
    assert asyncio.run(up.gateway_idle()) is False


def test_rollout_slice_and_force(tmp_path, repos):
    up = updater(tmp_path, repos, Fleet(repos), hermes=fake_hermes(tmp_path, repos), pct=0)
    assert asyncio.run(up.check_and_apply()) == "not_in_rollout"
    assert head(repos) == repos["sha"]["v0.1.0"]
    assert asyncio.run(up.check_and_apply(force=True)) == "updated"


def test_opted_out_and_cloud_boxes_are_left_alone(tmp_path, repos, monkeypatch):
    up = updater(tmp_path, repos, Fleet(repos), hermes=fake_hermes(tmp_path, repos), cloud=True)
    assert asyncio.run(up.check_and_apply()) == "cloud_managed"
    assert up.status()["state"] == "cloud_managed"
    monkeypatch.setenv("HERALD_HERMES_AUTO_UPDATE", "0")
    up = updater(tmp_path, repos, Fleet(repos), hermes=fake_hermes(tmp_path, repos))
    assert asyncio.run(up.check_and_apply(force=True)) == "disabled"
    assert head(repos) == repos["sha"]["v0.1.0"]


def test_no_tested_release_in_the_manifest_does_nothing(tmp_path, repos):
    up = updater(tmp_path, repos, Fleet(repos), hermes=fake_hermes(tmp_path, repos))

    async def plain():
        return sign(manifest())

    up._fetch = plain
    assert asyncio.run(up.check_and_apply(force=True)) == "no_tested_release"


def test_resume_after_the_bridge_restarted_mid_update(tmp_path, repos):
    fleet = Fleet(repos)
    up = updater(tmp_path, repos, fleet, hermes="hermes")
    pin = pin_for(repos)
    hu.save_state(up.state_file, {"pending": {"to": pin, "from_sha": repos["sha"]["v0.1.0"],
                                              "from_ref": None, "units": ["hermes-gateway.service"]}})
    # Interrupted before the checkout moved: roll back (a no-op move) + mark.
    asyncio.run(up.resume())
    st = json.loads(up.state_file.read_text())
    assert "pending" not in st and st["bad"]["0.2.0"] == "interrupted"
    # Interrupted after a good install: just commit it.
    git(repos["co"], "checkout", "-q", "--detach", "v0.2.0")
    fleet.running = "0.2.0"
    hu.save_state(up.state_file, {"pending": {"to": pin, "from_sha": repos["sha"]["v0.1.0"]}})
    asyncio.run(up.resume())
    st = json.loads(up.state_file.read_text())
    assert "pending" not in st and st["last_result"].endswith("->v0.2.0")


def test_status_rides_on_tunnel_connect():
    from herald_relay.relay_client import HeraldRelayClient

    class H:
        def status(self):
            return {"enabled": True, "state": "skipped", "reason": "carried_patches"}

    c = HeraldRelayClient(relay_url="https://r", device_token="t",
                          local_hermes_url="http://127.0.0.1:1", hermes_key="k")
    c.hermes_updater = H()
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True}

    class Client:
        async def post(self, url, json):
            sent.update(json)
            return Resp()

    asyncio.run(c._register(Client()))
    assert sent["hermes_update"]["reason"] == "carried_patches"
