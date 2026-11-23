"""Herald Bridge updates itself (herald#505): signed manifest, staged install,
idle-gated swap, probation + rollback, opt-out, the app's door."""
import asyncio
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import bridge_update as bu  # noqa: E402
from herald_relay import is_local_door, local_bridge  # noqa: E402

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(
    serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
KEYS = {"test-key": PUB}
CUR = bu.current_version()


def _next(v: str) -> str:
    a, b, c = bu.vtuple(v)[:3]
    return f"{a}.{b}.{c + 1}"


NEW = _next(CUR)


def manifest(**over) -> dict:
    m = {"package": "hermes-herald", "version": NEW,
         "wheel_name": f"hermes_herald-{NEW}-py3-none-any.whl",
         "wheel_url": f"https://github.com/x/y/releases/download/v/hermes_herald-{NEW}-py3-none-any.whl",
         "sha256": "a" * 64, "git_sha": "b" * 40, "min_supported": "0.7.0",
         "rollout_percent": 100, "expires": time.time() + 86400}
    m.update(over)
    return m


def sign(m: dict, key=KEY, keyid="test-key", ptype=bu.PAYLOAD_TYPE) -> dict:
    body = json.dumps(m, sort_keys=True).encode()
    sig = key.sign(bu.pae(ptype, body))
    return {"payloadType": ptype, "payload": base64.b64encode(body).decode(),
            "signatures": [{"keyid": keyid, "sig": base64.b64encode(sig).decode()}]}


# ── signature verification ───────────────────────────────────────────────────

def test_valid_manifest_verifies():
    assert bu.verify_envelope(sign(manifest()), keys=KEYS)["version"] == NEW


def test_tampered_payload_is_refused():
    env = sign(manifest())
    m = json.loads(base64.b64decode(env["payload"]))
    m["wheel_url"] = "https://evil.example/x.whl"
    env["payload"] = base64.b64encode(json.dumps(m, sort_keys=True).encode()).decode()
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(env, keys=KEYS)
    assert e.value.code == "bad_signature"


def test_other_key_is_refused():
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(sign(manifest(), key=Ed25519PrivateKey.generate()), keys=KEYS)
    assert e.value.code == "bad_signature"


def test_signature_covers_payload_type():
    """DSSE PAE binds the type: a signature over another type does not verify."""
    env = sign(manifest(), ptype="application/json")
    env["payloadType"] = bu.PAYLOAD_TYPE
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(env, keys=KEYS)
    assert e.value.code == "bad_signature"


def test_unknown_keyid_is_only_a_hint():
    assert bu.verify_envelope(sign(manifest(), keyid="renamed"), keys=KEYS)["version"] == NEW


def test_urlsafe_base64_accepted():
    env = sign(manifest())
    env["payload"] = env["payload"].replace("+", "-").replace("/", "_").rstrip("=")
    assert bu.verify_envelope(env, keys=KEYS)["version"] == NEW


@pytest.mark.parametrize("env", [
    {}, {"payloadType": "x"}, {"payloadType": bu.PAYLOAD_TYPE, "payload": "", "signatures": []},
])
def test_malformed_envelopes_refused(env):
    with pytest.raises(bu.UpdateError):
        bu.verify_envelope(env, keys=KEYS)


def test_expired_manifest_refused():
    """TUF freeze attack: an old manifest replayed forever must stop working."""
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(sign(manifest(expires=time.time() - 1)), keys=KEYS)
    assert e.value.code == "expired"


@pytest.mark.parametrize("over", [
    {"wheel_url": "http://plain.example/x.whl"},
    {"wheel_name": "hermes_herald-9.9.9-py3-none-any.whl"},
    {"wheel_name": "../../evil.whl"},
    {"sha256": "xyz"},
    {"package": "other"},
    {"rollout_percent": 101},
    {"rollout_percent": True},
    {"version": "1.0.0; rm -rf /"},
])
def test_signed_but_invalid_fields_refused(over):
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(sign(manifest(**over)), keys=KEYS)
    assert e.value.code == "bad_manifest"


# ── distribution rename: hermes-herald -> herald-bridge ─────────────────────

@pytest.mark.parametrize("pkg,wheel", [
    ("hermes-herald", "hermes_herald"),
    ("herald-bridge", "herald_bridge"),
])
def test_both_distribution_names_accepted(pkg, wheel):
    m = manifest(package=pkg, wheel_name=f"{wheel}-{NEW}-py3-none-any.whl",
                 wheel_url=f"https://github.com/x/y/releases/download/v/{wheel}-{NEW}-py3-none-any.whl")
    got = bu.verify_envelope(sign(m), keys=KEYS)
    assert (got["package"], got["version"]) == (pkg, NEW)


@pytest.mark.parametrize("pkg,wheel", [
    ("hermes-herald", "herald_bridge"),  # wheel must match its manifest package
    ("herald-bridge", "hermes_herald"),
    ("herald_bridge", "herald_bridge"),  # package is the normalized dist name only
    ("herald-bridge", "herald_bridge_evil"),
])
def test_mismatched_or_unknown_names_refused(pkg, wheel):
    with pytest.raises(bu.UpdateError) as e:
        bu.verify_envelope(sign(manifest(package=pkg, wheel_name=f"{wheel}-{NEW}-py3-none-any.whl")),
                           keys=KEYS)
    assert e.value.code == "bad_manifest"


def test_releases_still_built_as_hermes_herald():
    # 0.8.x bridges only accept hermes-herald: the build must not switch until
    # a release carrying PACKAGES (this one) is at 100%.
    assert bu.PACKAGE == "hermes-herald"
    assert set(bu.PACKAGES) == {"hermes-herald", "herald-bridge"}
    text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    assert 'name = "hermes-herald"' in text


def test_current_version_found_under_either_name(monkeypatch):
    import importlib.metadata as md

    def fake(name):
        if name == "herald-bridge":
            return "1.2.3"
        raise md.PackageNotFoundError(name)
    monkeypatch.setattr(md, "version", fake)
    assert bu.current_version() == "1.2.3"


def test_compiled_in_key_is_real():
    k = base64.b64decode(next(iter(bu.TRUSTED_KEYS.values())))
    assert len(k) == 32


# ── rollout + opt-out ────────────────────────────────────────────────────────

def test_rollout_bucket_is_stable_and_bounded():
    b = bu.rollout_bucket("tok", "1.2.3")
    assert b == bu.rollout_bucket("tok", "1.2.3") and 0 <= b < 100
    assert not bu.in_rollout("tok", manifest(rollout_percent=0))
    assert bu.in_rollout("tok", manifest(rollout_percent=100))
    share = sum(bu.in_rollout(f"t{i}", manifest(rollout_percent=10)) for i in range(2000)) / 2000
    assert 0.06 < share < 0.14


@pytest.mark.parametrize("env,cfg,out", [
    ("0", {}, True), ("false", {}, True), ("", {"auto_update": False}, True),
    ("", {"auto_update": "off"}, True), ("1", {}, False), ("", {}, False),
])
def test_opt_out(monkeypatch, env, cfg, out):
    monkeypatch.setenv("HERALD_BRIDGE_AUTO_UPDATE", env)
    assert bu.opted_out(cfg) is out


# ── layout, swap, stage ──────────────────────────────────────────────────────

def test_layout_only_for_venvs_we_own(tmp_path, monkeypatch):
    monkeypatch.delenv("HERALD_BRIDGE_ROOT", raising=False)
    lay = bu.Layout.detect(str(tmp_path / ".herald-relay"), home=tmp_path, in_venv=True)
    assert lay.root == tmp_path / ".herald-relay"
    assert lay.versions == tmp_path / ".herald-relay-versions"
    again = bu.Layout.detect(str(tmp_path / ".herald-relay-versions" / "0.9.0"),
                             home=tmp_path, in_venv=True)
    assert again == lay
    cloud = bu.Layout.detect(str(tmp_path / ".herald" / "venv"), home=tmp_path, in_venv=True)
    assert cloud.versions == tmp_path / ".herald" / "venv-versions"
    assert bu.Layout.detect(str(tmp_path / ".hermes/hermes-agent/venv"), home=tmp_path,
                            in_venv=True) is None
    assert bu.Layout.detect(str(tmp_path / ".herald-relay"), home=tmp_path, in_venv=False) is None
    monkeypatch.setenv("HERALD_BRIDGE_ROOT", str(tmp_path / "custom"))
    assert bu.Layout.detect("/anywhere", home=tmp_path, in_venv=True).root == tmp_path / "custom"


def _venv(path: Path, version: str) -> Path:
    (path / "bin").mkdir(parents=True)
    (path / "VERSION").write_text(version)
    return path


def test_swap_first_update_keeps_original_then_flips_back(tmp_path):
    lay = bu.Layout(tmp_path / ".herald-relay", tmp_path / ".herald-relay-versions")
    _venv(lay.root, "0.8.0")
    new = _venv(lay.versions / "0.8.1", "0.8.1")
    prev = bu.swap(lay, new, from_version="0.8.0")
    assert lay.root.is_symlink() and (lay.root / "VERSION").read_text() == "0.8.1"
    assert prev == lay.versions / "0.8.0-original" and (prev / "VERSION").read_text() == "0.8.0"
    back = bu.swap(lay, prev)
    assert back == new.resolve() and (lay.root / "VERSION").read_text() == "0.8.0"
    assert not (tmp_path / ".herald-relay.herald-swap").exists()


def _ok(stdout=""):
    return subprocess.CompletedProcess([], 0, stdout, "")


def test_stage_installs_beside_live_and_selfchecks(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "_uv", lambda: "")
    lay = bu.Layout(tmp_path / ".herald-relay", tmp_path / ".herald-relay-versions")
    _venv(lay.root, CUR)
    seen = []

    def runner(cmd):
        seen.append(cmd)
        if cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3], "bin").mkdir(parents=True)
        if cmd[-1] == "selfcheck":
            return _ok(json.dumps({"version": NEW}))
        return _ok()

    out = bu.stage(lay, manifest(), tmp_path / "w.whl", runner=runner)
    assert out == lay.versions / NEW and out.is_dir()
    assert seen[-1] == [str(out / "bin" / "herald-tunnel"), "selfcheck"]
    assert (lay.root / "VERSION").read_text() == CUR  # live venv untouched


def test_stage_failed_selfcheck_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "_uv", lambda: "")
    lay = bu.Layout(tmp_path / ".herald-relay", tmp_path / ".herald-relay-versions")

    def runner(cmd):
        if cmd[1:3] == ["-m", "venv"]:
            Path(cmd[3], "bin").mkdir(parents=True)
        if cmd[-1] == "selfcheck":
            return subprocess.CompletedProcess(cmd, 1, "", "ImportError")
        return _ok()

    with pytest.raises(bu.UpdateError) as e:
        bu.stage(lay, manifest(), tmp_path / "w.whl", runner=runner)
    assert e.value.code == "selfcheck_failed"
    assert not (lay.versions / NEW).exists()


def test_stage_failed_install(tmp_path, monkeypatch):
    monkeypatch.setattr(bu, "_uv", lambda: "/usr/bin/uv")
    lay = bu.Layout(tmp_path / ".herald-relay", tmp_path / ".herald-relay-versions")
    with pytest.raises(bu.UpdateError) as e:
        bu.stage(lay, manifest(), tmp_path / "w.whl",
                 runner=lambda c: subprocess.CompletedProcess(c, 2, "", "no network"))
    assert e.value.code == "install_failed"


# ── the updater: idle gating, restart, rollback ──────────────────────────────

class Rig:
    def __init__(self, tmp_path, *, busy=lambda: False, env=None, probation=False,
                 reachable=True):
        self.lay = bu.Layout(tmp_path / ".herald-relay", tmp_path / ".herald-relay-versions")
        if not self.lay.root.exists():
            _venv(self.lay.root, CUR)
        self.state = tmp_path / "state.json"
        self.restarts = []
        self.env = env if env is not None else sign(manifest())

        async def fetch():
            return self.env

        async def download(m):
            return tmp_path / m["wheel_name"]

        def stager(lay, m, wheel):
            return _venv(lay.versions / m["version"], m["version"])

        async def relay_up():
            return reachable

        self.u = bu.BridgeUpdater(
            relay_url="https://relay.test", device_token="tok", busy=busy, layout=self.lay,
            state_file=self.state, restart=self.restarts.append, fetch=fetch,
            download=download, stager=stager, keys=KEYS, probation=probation,
            relay_reachable=relay_up)


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(bu, "IDLE_FOR_S", 0.05)
    monkeypatch.setattr(bu, "IDLE_POLL_S", 0.01)
    monkeypatch.setattr(bu, "PROBATION_S", 0.05)
    monkeypatch.delenv("HERALD_BRIDGE_AUTO_UPDATE", raising=False)


def test_update_swaps_and_restarts_when_idle(tmp_path, fast):
    rig = Rig(tmp_path)
    assert asyncio.run(rig.u.check_and_apply()) == "restarting"
    assert rig.restarts == [rig.lay]
    assert (rig.lay.root / "VERSION").read_text() == NEW
    p = bu.load_state(rig.state)["pending"]
    assert p["from_version"] == CUR and p["to_version"] == NEW and p["boots"] == 0
    assert Path(p["from_path"], "VERSION").read_text() == CUR


def test_no_restart_while_a_run_is_in_flight(tmp_path, fast):
    busy = {"on": True}
    rig = Rig(tmp_path, busy=lambda: busy["on"])

    async def go():
        t = asyncio.create_task(rig.u.check_and_apply())
        await asyncio.sleep(0.3)
        assert rig.u.state == "waiting_idle"
        assert rig.restarts == [] and not rig.lay.root.is_symlink()
        busy["on"] = False
        return await asyncio.wait_for(t, 2)

    assert asyncio.run(go()) == "restarting"
    assert rig.restarts == [rig.lay]


def test_busy_reflects_runs_forwards_and_approvals():
    from herald_relay.relay_client import HeraldRelayClient
    c = HeraldRelayClient("https://r", "tok", "http://h")
    assert not c.busy()
    c._inflight_forwards = 1
    assert c.busy()
    c._inflight_forwards = 0
    c._approval_queues["r"] = object()
    assert c.busy()
    c._approval_queues.clear()
    c._run_tasks["r"] = object()
    assert c.busy()


def test_not_newer_is_current(tmp_path, fast):
    rig = Rig(tmp_path, env=sign(manifest(version=CUR, wheel_name=f"hermes_herald-{CUR}-py3-none-any.whl")))
    assert asyncio.run(rig.u.check_and_apply(force=True)) == "current"
    assert rig.restarts == []


def test_rollout_slice_and_bad_versions(tmp_path, fast):
    rig = Rig(tmp_path, env=sign(manifest(rollout_percent=0)))
    assert asyncio.run(rig.u.check_and_apply()) == "not_in_rollout"
    rig2 = Rig(tmp_path / "b")
    bu.save_state({"bad": {NEW: "crash_loop"}}, rig2.state)
    assert asyncio.run(rig2.u.check_and_apply()) == "skipped_bad"
    # "Update now" from the app overrides both.
    assert asyncio.run(rig.u.check_and_apply(force=True)) == "restarting"


def test_bad_signature_never_installs(tmp_path, fast):
    rig = Rig(tmp_path, env=sign(manifest(), key=Ed25519PrivateKey.generate()))
    assert asyncio.run(rig.u.check_and_apply(force=True)) == "bad_signature"
    assert rig.u.state == "failed" and rig.restarts == []
    assert not rig.lay.versions.exists()


def test_opt_out_refuses_update_now(tmp_path, fast, monkeypatch):
    monkeypatch.setenv("HERALD_BRIDGE_AUTO_UPDATE", "0")
    rig = Rig(tmp_path)
    st = rig.u.request_now()
    assert st["auto_update"] is False and st["state"] == "disabled"

    async def loop():
        monkeypatch.setattr(bu, "FIRST_CHECK_S", 0)
        monkeypatch.setattr(bu, "REQUEST_POLL_S", 0.01)
        t = asyncio.create_task(rig.u.run_forever())
        await asyncio.sleep(0.05)
        rig.u.request_now()
        await asyncio.sleep(0.1)
        t.cancel()

    asyncio.run(loop())
    assert rig.restarts == [] and not rig.lay.versions.exists()


def test_crash_loop_rolls_back_on_boot(tmp_path, fast):
    rig = Rig(tmp_path)
    asyncio.run(rig.u.check_and_apply())
    for _ in range(bu.MAX_PROBATION_BOOTS):
        assert bu.on_boot(rig.lay, path=rig.state, version=NEW) == "probation"
    assert bu.on_boot(rig.lay, path=rig.state, version=NEW) == "rolled_back"
    assert (rig.lay.root / "VERSION").read_text() == CUR
    st = bu.load_state(rig.state)
    assert "pending" not in st and st["bad"][NEW] == "crash_loop"


def test_boot_on_other_version_clears_pending(tmp_path, fast):
    rig = Rig(tmp_path)
    asyncio.run(rig.u.check_and_apply())
    assert bu.on_boot(rig.lay, path=rig.state, version=CUR) is None
    assert "pending" not in bu.load_state(rig.state)


def test_registering_commits_and_prunes(tmp_path, fast):
    rig = Rig(tmp_path)
    old = _venv(rig.lay.versions / "0.0.1", "0.0.1")
    asyncio.run(rig.u.check_and_apply())
    u2 = Rig(tmp_path, probation=True).u
    u2.on_registered({"ok": True})
    st = bu.load_state(rig.state)
    assert "pending" not in st and st["last_result"].startswith("updated:")
    assert not old.exists()                                   # pruned
    assert (rig.lay.versions / NEW).exists()                  # current kept
    assert (rig.lay.versions / f"{CUR}-original").exists()    # rollback target kept


def test_probation_rolls_back_when_relay_is_up_but_we_cannot_register(tmp_path, fast):
    rig = Rig(tmp_path)
    asyncio.run(rig.u.check_and_apply())
    rig2 = Rig(tmp_path, probation=True, reachable=True)
    asyncio.run(asyncio.wait_for(rig2.u._probation_watchdog(), 2))
    assert rig2.restarts == [rig2.lay]
    assert (rig.lay.root / "VERSION").read_text() == CUR
    assert bu.load_state(rig.state)["bad"][NEW] == "no_register"


def test_probation_waits_out_a_relay_outage(tmp_path, fast):
    rig = Rig(tmp_path)
    asyncio.run(rig.u.check_and_apply())
    rig2 = Rig(tmp_path, probation=True, reachable=False)

    async def go():
        t = asyncio.create_task(rig2.u._probation_watchdog())
        await asyncio.sleep(0.2)
        assert rig2.restarts == [] and not t.done()
        rig2.u.on_registered({})
        await asyncio.wait_for(t, 1)

    asyncio.run(go())
    assert (rig.lay.root / "VERSION").read_text() == NEW
    assert "pending" not in bu.load_state(rig.state)


def test_relay_hint_triggers_a_check(tmp_path, fast):
    rig = Rig(tmp_path)
    rig.u._wake = asyncio.Event()
    rig.u.on_registered({"plugin": {"latest": NEW}})
    assert rig.u._wake.is_set() and rig.u._force is False
    rig.u._wake.clear()
    # Streams reconnect every few minutes: the same hint acts only once.
    rig.u.on_registered({"plugin": {"latest": NEW}})
    assert not rig.u._wake.is_set()
    rig.u.on_registered({"plugin": {"latest": CUR}})
    assert not rig.u._wake.is_set()


def test_download_checks_sha256_and_size(tmp_path, monkeypatch):
    import httpx
    import respx
    body = b"wheel-bytes"
    good = manifest(sha256=hashlib.sha256(body).hexdigest())
    rig = Rig(tmp_path)
    u = bu.BridgeUpdater(relay_url="https://relay.test", device_token="t", busy=lambda: False,
                         layout=rig.lay, state_file=rig.state, keys=KEYS)
    with respx.mock:
        respx.get(good["wheel_url"]).mock(return_value=httpx.Response(200, content=body))
        p = asyncio.run(u._download_wheel(good))
        assert p.read_bytes() == body and p.name == good["wheel_name"]
        p.unlink()
        with pytest.raises(bu.UpdateError) as e:
            asyncio.run(u._download_wheel(manifest(sha256="c" * 64)))
        assert e.value.code == "hash_mismatch"
        assert not list((rig.lay.versions / ".cache").glob("*.whl*"))
        monkeypatch.setattr(bu, "MAX_WHEEL_BYTES", 4)
        with pytest.raises(bu.UpdateError) as e:
            asyncio.run(u._download_wheel(good))
        assert e.value.code == "download_too_big"


# ── the app's door and the relay's "update now" ──────────────────────────────

def test_bridge_door_routes():
    for p in ("/_herald/bridge", "/_herald/bridge/update", "/p/work/_herald/bridge/update"):
        assert local_bridge.matches(p) and is_local_door(p)
    assert not local_bridge.matches("/_herald/bridgeX")
    assert not local_bridge.matches("/v1/models")


def _door(monkeypatch, owns=True):
    async def caller_owns(url, prefix, headers):
        return owns
    monkeypatch.setattr(local_bridge, "_caller_owns", caller_owns)


def test_bridge_door_needs_the_agents_key(monkeypatch, tmp_path, fast):
    _door(monkeypatch, owns=False)
    s, b = asyncio.run(local_bridge.handle("POST", "/_herald/bridge/update", {}, None,
                                           hermes_url="http://h", updater=Rig(tmp_path).u))
    assert s == 401 and b["error"] == "agent_key_required"


def test_bridge_door_update_now(monkeypatch, tmp_path, fast):
    _door(monkeypatch)
    rig = Rig(tmp_path)
    rig.u._wake = asyncio.Event()
    s, b = asyncio.run(local_bridge.handle("POST", "/_herald/bridge/update", {}, None,
                                           hermes_url="http://h", updater=rig.u))
    assert s == 200 and b["state"] == "checking" and b["version"] == CUR
    assert rig.u._wake.is_set() and rig.u._force is True
    s, b = asyncio.run(local_bridge.handle("GET", "/_herald/bridge", {}, None,
                                           hermes_url="http://h", updater=rig.u))
    assert s == 200 and b["auto_update"] is True
    s, b = asyncio.run(local_bridge.handle("GET", "/_herald/bridge/update", {}, None,
                                           hermes_url="http://h", updater=rig.u))
    assert s == 405


def test_bridge_door_without_updater(monkeypatch):
    _door(monkeypatch)
    s, b = asyncio.run(local_bridge.handle("POST", "/_herald/bridge/update", {}, None,
                                           hermes_url="http://h", updater=None))
    assert s == 200 and b["state"] == "not_managed" and b["auto_update"] is False


def test_relay_push_triggers_update_now(tmp_path, fast):
    from herald_relay.relay_client import HeraldRelayClient
    c = HeraldRelayClient("https://r", "tok", "http://h")
    rig = Rig(tmp_path)
    rig.u._wake = asyncio.Event()
    c.updater = rig.u
    asyncio.run(c._handle_payload(None, {"type": "bridge_update", "force": True}))
    assert rig.u._wake.is_set() and rig.u._force is True


def test_register_reports_plugin_version(tmp_path, fast):
    from herald_relay.relay_client import HeraldRelayClient
    sent = {}

    class Resp:
        status_code = 200

        def json(self):
            return {"ok": True, "plugin": {"latest": NEW}}

    class Client:
        async def post(self, url, json=None):
            sent.update(json)
            return Resp()

    c = HeraldRelayClient("https://r", "tok", "http://h")
    rig = Rig(tmp_path)
    rig.u._wake = asyncio.Event()
    c.updater = rig.u
    asyncio.run(c._register(Client()))
    assert sent["plugin_version"] == CUR
    assert rig.u.registered.is_set() and rig.u._wake.is_set()


def test_selfcheck_prints_version(capsys):
    from herald_relay import tunnel_cli
    assert tunnel_cli.cmd_selfcheck(None) == 0
    assert json.loads(capsys.readouterr().out.strip())["version"] == CUR


def test_release_script_signs_what_the_plugin_accepts(tmp_path):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import release_plugin
    pem = KEY.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    env = release_plugin.sign_envelope(manifest(), pem, key_id="test-key")
    assert bu.verify_envelope(env, keys=KEYS)["version"] == NEW
    with pytest.raises(bu.UpdateError):
        bu.verify_envelope(env)  # a test key is not the production key
