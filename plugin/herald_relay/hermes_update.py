"""Herald Bridge keeps Hermes itself up to date, on a version Herald tested.

Hermes has no automatic update. With a hundred customer machines, "run
`hermes update`" by hand does not scale, and blindly following upstream means
every customer gets each release the day it ships, before anyone checked that
the Herald app still works with it. So the bridge (which already runs outside
the Hermes gateway, as its own systemd unit) does it:

1. **Signed pin.** The same signed release manifest the bridge updates itself
   from (``bridge_update.py``) may carry a ``hermes`` block::

       "hermes": {"version": "0.23.3", "tag": "v0.23.3",
                  "git_sha": "<40 hex>", "rollout_percent": 10}

   It names the one upstream release Herald tested. It is signed offline with
   the same key (``scripts/release_plugin.py --hermes-tag``), so a
   compromised relay cannot pick a Hermes version. No block = nothing happens.
2. **Only boxes we can update safely.** A source checkout of the official
   repository, run by a ``hermes-gateway`` systemd user unit, on a clean tree
   with no local commits. A box that carries patches (local commits or an
   uncommitted change) is skipped and reported, never clobbered. Herald Cloud
   (image-managed) and opted-out boxes are skipped too.
3. **Never past what we tested, never backwards.** The upstream tag must point
   at the signed commit (git hashes make that a checksum of the whole tree);
   the box must be strictly behind it; and if upstream has already published a
   newer stable release than the tested one, we wait, because ``hermes
   update`` would go to that one.
4. **Staged.** The release is fetched and checked out into a separate git
   worktree and compiled with this box's interpreter before anything live is
   touched.
5. **Idle.** Applied only when the gateway reports no active agent, API run or
   delegation (``GET /health/detailed``, falling back to
   ``gateway_state.json``) and the bridge has nothing in flight, continuously
   for ``IDLE_FOR_S``.
6. **Hermes installs it.** ``hermes update --yes --channel stable
   --no-gateway-restart`` does the actual install: its dependency manager,
   config migrations, downgrade guard and locks. We then check it landed on
   exactly the signed commit.
7. **Restart + health check.** The bridge restarts the gateway unit(s)
   through the user's systemd, then checks local ``/health`` reports the new
   version and ``/v1/models`` answers with the key, and through the relay (the
   phone's path) ``/health``, ``/api/soul`` and ``/v1/skills``.
8. **Rollback.** Any failure puts the checkout back on the previous commit,
   restarts again, re-checks health and marks the version bad for this box.
   A failed rollback is reported loudly (``rollback_failed``) and nothing is
   retried until an operator looks.

Opt out: ``HERALD_HERMES_AUTO_UPDATE=0`` in the tunnel's environment or
``herald.hermes_auto_update: false`` in config.yaml. The state (current
version, target, why it was skipped, last result) goes to the relay on every
connect so ops can see the fleet at ``GET /relay/plugin/versions``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from . import bridge_update as bu

logger = logging.getLogger("herald.hermes_update")

OFFICIAL_REPO = re.compile(r"(?:github\.com[:/])NousResearch/hermes-agent(?:\.git)?/?$", re.I)
_STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+$")

CHECK_EVERY_S = float(os.environ.get("HERALD_HERMES_CHECK_S", str(6 * 3600)))
JITTER_S = float(os.environ.get("HERALD_HERMES_JITTER_S", "1800"))
FIRST_CHECK_S = float(os.environ.get("HERALD_HERMES_FIRST_CHECK_S", "600"))
IDLE_FOR_S = float(os.environ.get("HERALD_HERMES_IDLE_S", "120"))
IDLE_POLL_S = 5.0
IDLE_GIVE_UP_S = float(os.environ.get("HERALD_HERMES_IDLE_GIVE_UP_S", str(3 * 3600)))
UPDATE_TIMEOUT_S = 40 * 60
HEALTH_WAIT_S = float(os.environ.get("HERALD_HERMES_HEALTH_WAIT_S", "240"))
HEALTH_POLL_S = 5.0
_OFF = ("0", "false", "no", "off")


# ── the signed pin ───────────────────────────────────────────────────────────

def pin_from(manifest: dict) -> Optional[dict]:
    """The validated ``hermes`` block of a VERIFIED manifest, or None.
    Malformed = None: a bad block must never stop the bridge's own update."""
    h = manifest.get("hermes") if isinstance(manifest, dict) else None
    if not isinstance(h, dict):
        return None
    tag, sha, ver = h.get("tag"), h.get("git_sha"), h.get("version")
    pct = h.get("rollout_percent", 0)
    if not (isinstance(tag, str) and _STABLE_TAG.match(tag)):
        return None
    if not (isinstance(sha, str) and _SHA40.match(sha)):
        return None
    if not (isinstance(ver, str) and _VERSION.match(ver) and tag == f"v{ver}"):
        return None
    if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
        return None
    return {"tag": tag, "git_sha": sha, "version": ver, "rollout_percent": pct}


def in_rollout(device_token: str, pin: dict) -> bool:
    h = hashlib.sha256(f"{device_token}:hermes:{pin['version']}".encode()).hexdigest()
    return int(h[:8], 16) % 100 < pin["rollout_percent"]


def opted_out(cfg: Optional[dict] = None) -> bool:
    if os.environ.get("HERALD_HERMES_AUTO_UPDATE", "").strip().lower() in _OFF:
        return True
    v = (cfg or {}).get("hermes_auto_update")
    return v is False or (isinstance(v, str) and v.strip().lower() in _OFF)


def _stable(tag: str) -> Optional[tuple[int, int, int]]:
    m = _STABLE_TAG.match(tag)
    return (int(m[1]), int(m[2]), int(m[3])) if m else None


# ── the box ──────────────────────────────────────────────────────────────────

Runner = Callable[..., subprocess.CompletedProcess]


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: float = 120,
         env: Optional[dict] = None) -> subprocess.CompletedProcess:
    base = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    base.update(env or {})
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL, env=base)


class Skip(Exception):
    """Not updating this box now; ``code`` says why (safe to show and report)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class Box:
    """The Hermes install this bridge would update."""
    root: Path                       # the git checkout
    hermes: str                      # the `hermes` launcher
    python: str = ""                 # Hermes' interpreter, for the compile check
    remote: str = "origin"
    units: list[str] = field(default_factory=list)
    run: Runner = _run

    def git(self, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
        return self.run(["git", "-C", str(self.root), *args], timeout=timeout)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").stdout.strip()

    def ref(self) -> Optional[str]:
        """The branch HEAD is on, or None when detached (a release tag)."""
        r = self.git("symbolic-ref", "-q", "--short", "HEAD")
        return r.stdout.strip() or None if r.returncode == 0 else None


def detect_box(*, run: Runner = _run, hermes_home: Optional[Path] = None,
               root: Optional[Path] = None, hermes: Optional[str] = None,
               python: Optional[str] = None) -> Box:
    """Find the Hermes checkout and gateway units, or raise Skip."""
    if root is None or hermes is None or python is None:
        from . import tunnel_cli  # noqa: PLC0415
        if hermes is None:
            launchers = tunnel_cli._hermes_launchers()
            hermes = launchers[0] if launchers else ""
        cmd = tunnel_cli._hermes_command()
        if python is None:
            python = cmd[0] if cmd else ""
        if root is None:
            # 0.23+: `[py, -I, -c, BOOT, <checkout>]`; older: a venv inside it.
            if len(cmd) >= 5:
                root = Path(cmd[4])
            elif python:
                # <checkout>/venv/bin/python. Not resolve(): that follows the
                # venv's symlink to the base interpreter.
                root = Path(python).parent.parent.parent
            else:
                root = Path(hermes_home or Path.home() / ".hermes") / "hermes-agent"
    if not hermes:
        raise Skip("hermes_not_found")
    if not (Path(root) / ".git").exists():
        raise Skip("not_git_checkout", str(root))
    box = Box(root=Path(root), hermes=hermes, python=python or "", run=run)
    remotes = box.git("remote", "-v").stdout.splitlines()
    official = [ln.split()[0] for ln in remotes
                if len(ln.split()) >= 2 and OFFICIAL_REPO.search(ln.split()[1])]
    if not official:
        raise Skip("unofficial_origin")
    box.remote = "origin" if "origin" in official else official[0]
    r = run(["systemctl", "--user", "list-units", "hermes-gateway*.service", "--all",
             "--plain", "--no-legend", "--no-pager"], timeout=30)
    if r.returncode != 0:
        raise Skip("no_user_systemd")
    box.units = [ln.split()[0] for ln in r.stdout.splitlines()
                 if ln.split() and ln.split()[0].endswith(".service")
                 and " active " in f" {ln} "]
    if not box.units:
        raise Skip("no_gateway_unit")
    return box


def preflight(box: Box, pin: dict) -> dict:
    """Read-only checks (plus a tag fetch). Returns facts; raises Skip."""
    if box.git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise Skip("local_changes", "uncommitted changes in the Hermes checkout")
    local = box.git("rev-list", "--count", "HEAD", "--not", "--remotes").stdout.strip()
    if local and local != "0":
        raise Skip("carried_patches", f"{local} local commit(s) not on any remote")
    head = box.head()
    if head == pin["git_sha"]:
        raise Skip("current")
    f = box.git("fetch", "--quiet", "--no-tags", "--force", box.remote,
                f"refs/tags/{pin['tag']}:refs/tags/{pin['tag']}", timeout=600)
    if f.returncode != 0:
        raise Skip("fetch_failed", (f.stderr or "")[-200:])
    tagged = box.git("rev-parse", f"{pin['tag']}^{{commit}}").stdout.strip()
    if tagged != pin["git_sha"]:
        # Upstream moved the tag, or something rewrote it on the way here.
        raise Skip("tag_mismatch", f"{pin['tag']} is {tagged[:12]}, signed {pin['git_sha'][:12]}")
    if box.git("merge-base", "--is-ancestor", head, pin["git_sha"]).returncode != 0:
        raise Skip("ahead_or_diverged", "this checkout is not behind the tested release")
    tags = box.git("ls-remote", "--tags", "--refs", box.remote, "refs/tags/v*", timeout=120)
    stable = [t for t in (ln.rsplit("refs/tags/", 1)[-1] for ln in tags.stdout.splitlines())
              if _stable(t)]
    newest = max(stable, key=_stable, default=pin["tag"])
    if _stable(newest) > _stable(pin["tag"]):
        # `hermes update` follows the stable channel to the newest release;
        # Herald hasn't tested that one yet.
        raise Skip("upstream_ahead_of_tested", f"upstream {newest}, tested {pin['tag']}")
    return {"from_sha": head, "from_ref": box.ref()}


def stage(box: Box, pin: dict, stage_root: Path) -> None:
    """Check the release out into its own worktree and compile it with this
    box's interpreter. Never touches the live checkout. Removed afterwards."""
    wt = stage_root / pin["git_sha"][:12]
    box.git("worktree", "remove", "--force", str(wt))
    shutil.rmtree(wt, ignore_errors=True)
    stage_root.mkdir(parents=True, exist_ok=True)
    try:
        r = box.git("worktree", "add", "--detach", str(wt), pin["git_sha"], timeout=300)
        if r.returncode != 0:
            raise Skip("stage_failed", (r.stderr or "")[-200:])
        if box.python:
            r = box.run([box.python, "-m", "compileall", "-q", "-j", "0",
                         str(wt / "gateway"), str(wt / "hermes_cli")], timeout=600)
            if r.returncode != 0:
                raise Skip("stage_failed", "does not compile on this interpreter")
    finally:
        box.git("worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)


# ── state across restarts ────────────────────────────────────────────────────

def state_path(hermes_home: Path) -> Path:
    return hermes_home / "herald" / "hermes-update.json"


def load_state(p: Path) -> dict:
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(p: Path, st: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, p)


# ── the updater ──────────────────────────────────────────────────────────────

HttpGet = Callable[[str, dict], Awaitable[tuple[int, Any]]]


async def _http_get(url: str, headers: dict) -> tuple[int, Any]:
    import httpx  # noqa: PLC0415
    try:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(url, headers=headers)
        try:
            return r.status_code, r.json()
        except ValueError:
            return r.status_code, None
    except Exception:  # noqa: BLE001
        return 0, None


class HermesUpdater:
    """Runs inside ``herald-tunnel run`` next to the bridge's own updater."""

    def __init__(self, *, relay_url: str, device_token: str, api_key: str,
                 hermes_home: Path, cfg: Optional[dict] = None,
                 busy: Callable[[], bool] = lambda: False,
                 local_url: str = "http://127.0.0.1:8642",
                 cloud: bool = False,
                 fetch: Optional[Callable[[], Awaitable[dict]]] = None,
                 keys: Optional[dict[str, str]] = None,
                 detect: Callable[[], Box] = detect_box,
                 http_get: HttpGet = _http_get,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        self.relay_url = relay_url.rstrip("/")
        self.device_token = device_token
        self.api_key = api_key
        self.hermes_home = Path(hermes_home)
        self.cfg = cfg or {}
        self.busy = busy
        self.local_url = local_url.rstrip("/")
        self.cloud = cloud
        self._fetch = fetch or (lambda: bu.fetch_manifest(self.relay_url))
        self._keys = keys
        self._detect = detect
        self._get = http_get
        self._sleep = sleep
        self.state_file = state_path(self.hermes_home)
        self.state = "idle"
        self.reason: Optional[str] = None
        self.target: Optional[str] = None
        self.current: Optional[str] = None
        self.last_check: Optional[float] = None
        self._wake: Optional[asyncio.Event] = None
        self._force = False
        self._running = False

    # -- what the relay and the app see ---------------------------------------

    def enabled(self) -> tuple[bool, str]:
        if opted_out(self.cfg):
            return False, "disabled"
        if self.cloud:
            return False, "cloud_managed"
        return True, ""

    def status(self) -> dict:
        ok, why = self.enabled()
        st = load_state(self.state_file)
        return {"enabled": ok, "state": self.state if ok else why,
                "reason": self.reason if ok else why, "current": self.current,
                "target": self.target, "last_result": st.get("last_result"),
                "last_check": self.last_check, "bad": sorted(st.get("bad", {}))}

    def request_now(self) -> dict:
        """Ops asked: check now and skip the rollout slice (never the safety
        checks: carried patches, idle, health)."""
        self._force = True
        if self._wake is not None:
            self._wake.set()
        return self.status()

    # -- the loop ---------------------------------------------------------------

    async def run_forever(self) -> None:
        self._wake = asyncio.Event()
        await self.resume()
        deadline = time.monotonic() + FIRST_CHECK_S + random.uniform(0, 600)
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(),
                                       timeout=max(0.0, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            force, self._force = self._force, False
            if self.enabled()[0]:
                await self.check_and_apply(force=force)
            deadline = time.monotonic() + CHECK_EVERY_S + random.uniform(-JITTER_S, JITTER_S)

    async def check_and_apply(self, *, force: bool = False) -> str:
        if self._running:
            return "busy"
        self._running = True
        self.last_check = time.time()
        try:
            return await self._check_and_apply(force)
        except Skip as s:
            self.state = "current" if s.code == "current" else "skipped"
            self.reason = None if s.code == "current" else s.code
            self._record(s.code if s.code == "current" else f"skipped:{s.code}")
            if s.code != "current":
                logger.info("Hermes update skipped: %s", s)
            return s.code
        except bu.UpdateError as e:
            self.state, self.reason = "failed", e.code
            self._record(f"failed:{e.code}")
            return e.code
        except Exception as e:  # noqa: BLE001
            self.state, self.reason = "failed", type(e).__name__
            self._record(f"failed:{type(e).__name__}")
            logger.warning("Hermes update failed: %r", e)
            return "error"
        finally:
            self._running = False

    async def _check_and_apply(self, force: bool) -> str:
        ok, why = self.enabled()
        if not ok:
            raise Skip(why)
        st = load_state(self.state_file)
        if st.get("halted"):
            raise Skip("halted", st["halted"])
        self.state = "checking"
        await self._read_current()
        pin = pin_from(bu.verify_envelope(await self._fetch(), keys=self._keys))
        if pin is None:
            raise Skip("no_tested_release")
        self.target = pin["version"]
        if pin["version"] in st.get("bad", {}) and not force:
            raise Skip("known_bad", pin["version"])
        if not force and not in_rollout(self.device_token, pin):
            raise Skip("not_in_rollout")
        box = await asyncio.to_thread(self._detect)
        facts = await asyncio.to_thread(preflight, box, pin)
        self.state = "staging"
        await asyncio.to_thread(stage, box, pin, self.hermes_home / "herald" / "hermes-stage")
        self.state = "waiting_idle"
        await self.wait_idle()
        return await self.apply(box, pin, facts)

    async def apply(self, box: Box, pin: dict, facts: dict) -> str:
        """Install, restart, health check; roll back on any failure."""
        st = load_state(self.state_file)
        st["pending"] = {"to": pin, "from_sha": facts["from_sha"],
                         "from_ref": facts.get("from_ref"), "started": time.time(),
                         "units": box.units, "root": str(box.root)}
        save_state(self.state_file, st)
        self.state = "installing"
        logger.info("Updating Hermes to %s (tested by Herald)", pin["tag"])
        r = await asyncio.to_thread(
            box.run, [box.hermes, "update", "--yes", "--channel", "stable",
                      "--no-gateway-restart"], timeout=UPDATE_TIMEOUT_S,
            env={"HERMES_HOME": str(self.hermes_home)})
        landed = await asyncio.to_thread(box.head)
        if r.returncode != 0:
            return await self._roll_back(box, f"update_exit_{r.returncode}")
        if landed != pin["git_sha"]:
            return await self._roll_back(box, "unexpected_target")
        self.state = "restarting"
        await asyncio.to_thread(self._restart, box)
        bad = await self.health(pin["version"])
        if bad:
            return await self._roll_back(box, f"health:{bad}")
        st = load_state(self.state_file)
        p = st.pop("pending", {})
        st["last_good"] = {"version": pin["version"], "sha": pin["git_sha"], "at": time.time()}
        st["last_result"] = f"updated:{p.get('from_sha', '?')[:12]}->{pin['tag']}"
        save_state(self.state_file, st)
        self.state, self.reason, self.current = "current", None, pin["version"]
        logger.info("Hermes %s is healthy", pin["tag"])
        return "updated"

    async def _roll_back(self, box: Box, why: str) -> str:
        st = load_state(self.state_file)
        p = st.get("pending") or {}
        to = (p.get("to") or {}).get("version", "?")
        logger.warning("Hermes %s failed (%s); rolling back to %s", to, why,
                       (p.get("from_sha") or "?")[:12])
        self.state = "rolling_back"
        ok = await asyncio.to_thread(self._checkout_previous, box, p)
        if ok:
            await asyncio.to_thread(self._restart, box)
            bad = await self.health(None)
            ok = not bad
        st = load_state(self.state_file)
        st.pop("pending", None)
        st.setdefault("bad", {})[to] = why
        if ok:
            st["last_result"] = f"rolled_back:{why}"
            self.state, self.reason = "rolled_back", why
        else:
            # Leave the box alone from here on: an operator has to look.
            st["last_result"] = f"rollback_failed:{why}"
            st["halted"] = f"rollback_failed:{why}"
            self.state, self.reason = "rollback_failed", why
        save_state(self.state_file, st)
        return st["last_result"].split(":", 1)[0]

    def _checkout_previous(self, box: Box, p: dict) -> bool:
        sha, ref = p.get("from_sha"), p.get("from_ref")
        if not sha:
            return False
        if ref:
            # We checked there were no local commits, so the branch held
            # nothing that isn't on the remote: moving it back loses nothing.
            r = box.git("checkout", "-B", ref, sha, timeout=300)
        else:
            r = box.git("checkout", "--detach", sha, timeout=300)
        return r.returncode == 0 and box.head() == sha

    def _restart(self, box: Box) -> None:
        # The bridge is its own unit, outside the gateway's cgroup, so
        # restarting the gateway can't take the bridge (or this call) down.
        for u in box.units:
            box.run(["systemctl", "--user", "restart", u], timeout=300)

    async def health(self, version: Optional[str]) -> Optional[str]:
        """None when healthy, else the first failing check."""
        auth = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        deadline = time.monotonic() + HEALTH_WAIT_S
        last = "local_health"
        while time.monotonic() < deadline:
            code, body = await self._get(self.local_url + "/health", {})
            got = body.get("version") if isinstance(body, dict) else None
            if code == 200 and (version is None or got == version):
                break
            last = "local_health" if code != 200 else f"version_{got}"
            await self._sleep(HEALTH_POLL_S)
        else:
            return last
        checks = [("local_models", self.local_url + "/v1/models")]
        relay = f"{self.relay_url}/hermes/d/{self.device_token}"
        checks += [("relay_health", relay + "/health"), ("relay_soul", relay + "/api/soul"),
                   ("relay_skills", relay + "/v1/skills")]
        for name, url in checks:
            for _ in range(6):
                code, _body = await self._get(url, auth)
                if code == 200:
                    break
                await self._sleep(HEALTH_POLL_S)
            else:
                return name
        return None

    async def gateway_idle(self) -> bool:
        if self.busy():
            return False
        auth = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        code, body = await self._get(self.local_url + "/health/detailed", auth)
        if code == 200 and isinstance(body, dict) and "gateway_busy" in body:
            q = ((body.get("readiness") or {}).get("checks") or {}).get("background_queues") or {}
            return (not body.get("gateway_busy")
                    and not int(q.get("active_api_runs") or 0)
                    and not int(q.get("active_delegations") or 0))
        # Older Hermes: the runtime status file the gateway writes.
        try:
            d = json.loads((self.hermes_home / "gateway_state.json").read_text())
            return int(d.get("active_agents") or 0) == 0
        except (OSError, ValueError, TypeError):
            return False  # can't tell: treat as busy

    async def wait_idle(self) -> None:
        quiet_since: Optional[float] = None
        give_up = time.monotonic() + IDLE_GIVE_UP_S
        while True:
            now = time.monotonic()
            if now > give_up:
                raise Skip("never_idle")
            if not await self.gateway_idle():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= IDLE_FOR_S:
                return
            await self._sleep(IDLE_POLL_S)

    async def resume(self) -> None:
        """The bridge restarted mid-update: finish judging it."""
        st = load_state(self.state_file)
        p = st.get("pending")
        if not p:
            return
        try:
            box = await asyncio.to_thread(self._detect)
        except Skip:
            return
        pin = p.get("to") or {}
        head = await asyncio.to_thread(box.head)
        if head == pin.get("git_sha") and not await self.health(pin.get("version")):
            st.pop("pending", None)
            st["last_result"] = f"updated:{p.get('from_sha', '?')[:12]}->{pin.get('tag')}"
            save_state(self.state_file, st)
            self.state, self.current = "current", pin.get("version")
            return
        box.units = p.get("units") or box.units
        await self._roll_back(box, "interrupted")

    async def _read_current(self) -> None:
        code, body = await self._get(self.local_url + "/health", {})
        if code == 200 and isinstance(body, dict) and isinstance(body.get("version"), str):
            self.current = body["version"]

    def _record(self, result: str) -> None:
        try:
            st = load_state(self.state_file)
            st["last_check"] = time.time()
            st["last_result"] = result
            save_state(self.state_file, st)
        except OSError:
            pass
