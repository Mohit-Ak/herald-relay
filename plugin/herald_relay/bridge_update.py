"""Herald Bridge keeps itself up to date (herald#505).

Before this, a machine stayed on whatever version it was installed with, and
the app could only say "Herald Bridge on that computer is too old ... ask your
agent to update". Now the tunnel updates itself:

1. **Signed manifest.** ``GET <relay>/relay/plugin/manifest`` returns a DSSE
   envelope (https://github.com/secure-systems-lab/dsse) whose payload names
   one release: version, wheel URL + sha256, git commit, ``min_supported``,
   ``rollout_percent`` and ``expires``. It is signed OFFLINE at release time
   (``scripts/release_plugin.py``, key in Secret Manager
   ``herald-relay-signing-key``); the relay only serves the file, so a
   compromised relay cannot ship code. We verify against the Ed25519 key(s)
   compiled in below, refuse expired manifests (TUF's freeze attack) and never
   install a version that is not newer than the running one (rollback attack).
2. **Staged install.** The wheel is downloaded with a size cap, its sha256
   checked, and installed into a NEW venv next to the live one
   (``~/.herald-relay-versions/<version>``). The new venv must pass
   ``herald-tunnel selfcheck`` before anything is switched.
3. **Atomic swap at an idle moment.** The stable path every launcher uses
   (``~/.herald-relay``: systemd unit, keepalive script, skills) becomes a
   symlink, flipped with one ``rename(2)``. We only flip it when no run, no
   forwarded request and no approval has been in flight for ``IDLE_FOR_S``
   (Tailscale delays its updates the same way while a device is busy). Then
   the tunnel re-execs itself.
4. **Health check + rollback.** The new process is on probation until it
   registers with the relay. If it crash-loops (``MAX_PROBATION_BOOTS``) or
   cannot register while the relay itself is reachable, the symlink is
   flipped back, the version is marked bad and the old build is restarted.

Opt out (self-hosters who pin versions): ``HERALD_BRIDGE_AUTO_UPDATE=0`` in the
tunnel's environment, or ``herald.auto_update: false`` in config.yaml. That
also refuses "update now" from the app.

Only a venv Herald owns is ever touched: ``~/.herald-relay`` (local installs),
``~/.herald/venv`` (Herald Cloud) or ``HERALD_BRIDGE_ROOT``. A developer
checkout or a package installed into Hermes' own venv is left alone.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Optional

logger = logging.getLogger("herald.update")

PACKAGE = "hermes-herald"
PAYLOAD_TYPE = "application/vnd.herald.bridge-manifest+json"
MANIFEST_PATH = "/relay/plugin/manifest"

# keyid -> raw 32-byte Ed25519 public key (base64). The private half lives
# only in Secret Manager (herald-relay-signing-key). Rotate by adding the new
# key here, shipping a release signed with the OLD key, then signing with the
# new one and removing the old entry in a later release.
TRUSTED_KEYS: dict[str, str] = {
    "herald-bridge-2026-11": "pw2oKvc9QF9xQz0LZ/SrdHMe+EO6ZCltFwRTFhnC7F8=",
}

CHECK_EVERY_S = float(os.environ.get("HERALD_BRIDGE_CHECK_S", str(6 * 3600)))
JITTER_S = float(os.environ.get("HERALD_BRIDGE_JITTER_S", "1800"))
FIRST_CHECK_S = float(os.environ.get("HERALD_BRIDGE_FIRST_CHECK_S", "90"))
IDLE_FOR_S = float(os.environ.get("HERALD_BRIDGE_IDLE_S", "20"))
IDLE_POLL_S = 2.0
PROBATION_S = float(os.environ.get("HERALD_BRIDGE_PROBATION_S", "180"))
MAX_PROBATION_BOOTS = 3
MAX_WHEEL_BYTES = 20 * 1024 * 1024
INSTALL_TIMEOUT_S = 900
REQUEST_POLL_S = 30.0

_WHEEL_NAME = re.compile(r"^hermes_herald-(\d+(?:\.\d+){1,3})-py3-none-any\.whl$")
_VERSION = re.compile(r"^\d+(?:\.\d+){1,3}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-f]{7,40}$")


class UpdateError(Exception):
    """An update step refused or failed. ``code`` is short and safe to show."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


# ── versions ─────────────────────────────────────────────────────────────────

def current_version() -> str:
    try:
        from importlib.metadata import version  # noqa: PLC0415
        return version(PACKAGE)
    except Exception:  # noqa: BLE001  (not installed: a source checkout)
        from herald_relay import __version__  # noqa: PLC0415
        return __version__


def vtuple(v: str) -> tuple[int, ...]:
    m = re.match(r"^(\d+(?:\.\d+)*)", (v or "").strip())
    if not m:
        raise ValueError(f"not a version: {v!r}")
    parts = [int(x) for x in m.group(1).split(".")]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def newer(a: str, b: str) -> bool:
    """Is version *a* strictly newer than *b*?"""
    return vtuple(a) > vtuple(b)


# ── signed manifest ──────────────────────────────────────────────────────────

def pae(payload_type: str, body: bytes) -> bytes:
    """DSSE pre-authentication encoding: what the signature actually covers."""
    t = payload_type.encode()
    return b"DSSEv1 %d %s %d %s" % (len(t), t, len(body), body)


def _b64(s: str) -> bytes:
    s = (s or "").strip()
    pad = "=" * (-len(s) % 4)
    try:
        if "-" in s or "_" in s:
            return base64.urlsafe_b64decode(s + pad)
        return base64.b64decode(s + pad, validate=True)
    except Exception as e:  # noqa: BLE001
        raise UpdateError("bad_manifest", "base64") from e


def verify_envelope(envelope: dict, *, keys: Optional[dict[str, str]] = None,
                    now: Optional[float] = None) -> dict:
    """Return the manifest inside *envelope* if a trusted key signed it.

    Raises UpdateError(bad_manifest | bad_signature | expired). Fails closed:
    a missing crypto library is ``no_crypto``, never "skip the check".
    """
    try:
        from cryptography.exceptions import InvalidSignature  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: PLC0415
            Ed25519PublicKey,
        )
    except ImportError as e:
        raise UpdateError("no_crypto") from e

    keys = TRUSTED_KEYS if keys is None else keys
    if not isinstance(envelope, dict) or envelope.get("payloadType") != PAYLOAD_TYPE:
        raise UpdateError("bad_manifest", "payloadType")
    body = _b64(envelope.get("payload", ""))
    sigs = envelope.get("signatures")
    if not isinstance(sigs, list) or not sigs:
        raise UpdateError("bad_signature", "no signatures")
    signed = pae(PAYLOAD_TYPE, body)
    ok = False
    for s in sigs:
        if not isinstance(s, dict):
            continue
        # keyid is an unauthenticated hint: it only narrows which keys to try.
        kid = s.get("keyid") or ""
        candidates = [keys[kid]] if kid in keys else list(keys.values())
        try:
            sig = _b64(s.get("sig", ""))
        except UpdateError:
            continue
        for pub in candidates:
            try:
                Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(sig, signed)
                ok = True
                break
            except (InvalidSignature, ValueError):
                continue
        if ok:
            break
    if not ok:
        raise UpdateError("bad_signature")

    try:
        m = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise UpdateError("bad_manifest", "json") from e
    _check_manifest(m, now=time.time() if now is None else now)
    return m


def _check_manifest(m: dict, *, now: float) -> None:
    if not isinstance(m, dict):
        raise UpdateError("bad_manifest", "not an object")
    if m.get("package") != PACKAGE:
        raise UpdateError("bad_manifest", "package")
    v = m.get("version")
    if not isinstance(v, str) or not _VERSION.match(v):
        raise UpdateError("bad_manifest", "version")
    name = m.get("wheel_name")
    wm = _WHEEL_NAME.match(name) if isinstance(name, str) else None
    if not wm or wm.group(1) != v:
        raise UpdateError("bad_manifest", "wheel_name")
    url = m.get("wheel_url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise UpdateError("bad_manifest", "wheel_url")
    if not isinstance(m.get("sha256"), str) or not _SHA256.match(m["sha256"]):
        raise UpdateError("bad_manifest", "sha256")
    if "git_sha" in m and not (isinstance(m["git_sha"], str) and _GIT_SHA.match(m["git_sha"])):
        raise UpdateError("bad_manifest", "git_sha")
    ms = m.get("min_supported", "0.0.0")
    if not isinstance(ms, str) or not _VERSION.match(ms):
        raise UpdateError("bad_manifest", "min_supported")
    pct = m.get("rollout_percent", 100)
    if not isinstance(pct, int) or isinstance(pct, bool) or not 0 <= pct <= 100:
        raise UpdateError("bad_manifest", "rollout_percent")
    exp = m.get("expires")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        raise UpdateError("bad_manifest", "expires")
    if exp < now:
        raise UpdateError("expired")


def rollout_bucket(device_token: str, version: str) -> int:
    """Stable 0..99 per (machine, release): the same machine is always in or
    out of a given rollout step, and a different slice goes first next time."""
    h = hashlib.sha256(f"{device_token}:{version}".encode()).hexdigest()
    return int(h[:8], 16) % 100


def in_rollout(device_token: str, manifest: dict) -> bool:
    return rollout_bucket(device_token, manifest["version"]) < int(manifest.get("rollout_percent", 100))


def opted_out(cfg: Optional[dict] = None) -> bool:
    env = os.environ.get("HERALD_BRIDGE_AUTO_UPDATE", "").strip().lower()
    if env in ("0", "false", "no", "off"):
        return True
    v = (cfg or {}).get("auto_update")
    return v is False or (isinstance(v, str) and v.strip().lower() in ("0", "false", "no", "off"))


# ── where the venvs live ─────────────────────────────────────────────────────

@dataclass(frozen=True)
class Layout:
    root: Path      # the stable path every launcher uses, e.g. ~/.herald-relay
    versions: Path  # one venv per installed version, e.g. ~/.herald-relay-versions

    @property
    def bin(self) -> Path:
        return self.root / "bin" / "herald-tunnel"

    @classmethod
    def detect(cls, prefix: Optional[str] = None, *, home: Optional[Path] = None,
               in_venv: Optional[bool] = None) -> Optional["Layout"]:
        if in_venv is None:
            in_venv = sys.prefix != sys.base_prefix
        if not in_venv:
            return None
        p = Path(prefix or sys.prefix)
        explicit = os.environ.get("HERALD_BRIDGE_ROOT", "").strip()
        if explicit:
            root = Path(explicit).expanduser()
        else:
            # A venv we installed: <root>-versions/<version>.
            root = (p.parent.with_name(p.parent.name[: -len("-versions")])
                    if p.parent.name.endswith("-versions") else p)
            home = home or Path.home()
            if root not in (home / ".herald-relay", home / ".herald" / "venv"):
                return None
        return cls(root=root, versions=root.with_name(root.name + "-versions"))


def _base_python() -> str:
    base = getattr(sys, "_base_executable", "") or ""
    if base and os.access(base, os.X_OK) and Path(base).resolve() != Path(sys.executable).resolve():
        return base
    # pyvenv.cfg names the interpreter the venv was made from.
    try:
        for line in (Path(sys.prefix) / "pyvenv.cfg").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == "home":
                for n in (f"python{sys.version_info.major}.{sys.version_info.minor}", "python3"):
                    c = Path(v.strip()) / n
                    if os.access(c, os.X_OK):
                        return str(c)
    except OSError:
        pass
    return base or sys.executable


def _uv() -> str:
    for c in (shutil.which("uv"), str(Path.home() / ".local/bin/uv"),
              str(Path.home() / ".cargo/bin/uv")):
        if c and os.access(c, os.X_OK):
            return c
    return ""


Runner = Callable[[list[str]], subprocess.CompletedProcess]


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    return subprocess.run(cmd, capture_output=True, text=True, timeout=INSTALL_TIMEOUT_S, env=env)


def stage(layout: Layout, manifest: dict, wheel: Path, *, runner: Runner = _run) -> Path:
    """Install *wheel* into a fresh venv beside the live one and prove it
    starts. Returns the new venv. Never touches the live venv."""
    version = manifest["version"]
    target = layout.versions / version
    if target.exists() or target.is_symlink():
        if layout.root.exists() and target.resolve() == layout.root.resolve():
            raise UpdateError("already_current")
        shutil.rmtree(target, ignore_errors=True)
    layout.versions.mkdir(parents=True, exist_ok=True)
    py, uv = _base_python(), _uv()
    if uv:
        steps = [[uv, "venv", "-q", "--python", py, str(target)],
                 [uv, "pip", "install", "-q", "--python", str(target / "bin" / "python"), str(wheel)]]
    else:
        steps = [[py, "-m", "venv", str(target)],
                 [str(target / "bin" / "python"), "-m", "pip", "install", "-q",
                  "--disable-pip-version-check", "--no-input", str(wheel)]]
    try:
        for cmd in steps:
            r = runner(cmd)
            if r.returncode != 0:
                raise UpdateError("install_failed", (r.stderr or r.stdout or "")[-300:])
        r = runner([str(target / "bin" / "herald-tunnel"), "selfcheck"])
        try:
            got = json.loads((r.stdout or "").strip().splitlines()[-1]).get("version")
        except (ValueError, IndexError, AttributeError):
            got = None
        if r.returncode != 0 or got != version:
            raise UpdateError("selfcheck_failed", f"rc={r.returncode} version={got}")
    except UpdateError:
        shutil.rmtree(target, ignore_errors=True)
        raise
    except (OSError, subprocess.SubprocessError) as e:
        shutil.rmtree(target, ignore_errors=True)
        raise UpdateError("install_failed", type(e).__name__) from e
    return target


def swap(layout: Layout, target: Path, *, from_version: Optional[str] = None) -> Path:
    """Point ``layout.root`` at *target* in one rename(2). Returns what it
    pointed at before (the rollback target).

    The first update finds the original install as a real directory. It is
    moved under versions/ and kept as the rollback target; its scripts still
    name ``<root>/bin/python``, which is right again whenever root points back
    at it.
    """
    root = layout.root
    tmp = root.with_name(root.name + ".herald-swap")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    if root.is_symlink():
        previous = Path(os.path.realpath(root))
        os.replace(tmp, root)
        return previous
    layout.versions.mkdir(parents=True, exist_ok=True)
    previous = layout.versions / f"{from_version or current_version()}-original"
    n = 1
    while previous.exists() or previous.is_symlink():
        n += 1
        previous = layout.versions / f"{from_version or current_version()}-original-{n}"
    os.rename(root, previous)
    try:
        os.replace(tmp, root)
    except OSError:
        os.rename(previous, root)
        raise
    return previous


def prune(layout: Layout, keep: set[Path]) -> None:
    """Drop installed versions other than *keep* (current + previous)."""
    keep_real = {Path(os.path.realpath(k)) for k in keep}
    try:
        children = list(layout.versions.iterdir())
    except OSError:
        return
    for c in children:
        if c.name.startswith("."):
            continue
        if Path(os.path.realpath(c)) in keep_real:
            continue
        shutil.rmtree(c, ignore_errors=True)


# ── state across restarts ────────────────────────────────────────────────────

def state_path() -> Path:
    home = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
    return home / "herald" / "bridge-update.json"


def request_path() -> Path:
    return state_path().with_name("bridge-update.request")


def load_state(path: Optional[Path] = None) -> dict:
    try:
        d = json.loads((path or state_path()).read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(st: dict, path: Optional[Path] = None) -> None:
    p = path or state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=1, sort_keys=True))
    os.replace(tmp, p)


def rollback(layout: Layout, st: dict, reason: str, *, path: Optional[Path] = None) -> bool:
    p = st.get("pending") or {}
    prev = p.get("from_path")
    if not prev or not Path(prev).is_dir():
        st.pop("pending", None)
        st["last_error"] = f"rollback_impossible:{reason}"
        save_state(st, path)
        return False
    swap(layout, Path(prev))
    st.setdefault("bad", {})[p.get("to_version", "?")] = reason
    st.pop("pending", None)
    st["last_result"] = f"rolled_back:{reason}"
    st["rolled_back_at"] = time.time()
    save_state(st, path)
    logger.warning("Herald Bridge %s rolled back to %s (%s)",
                   p.get("to_version"), p.get("from_version"), reason)
    return True


def on_boot(layout: Optional[Layout], *, path: Optional[Path] = None,
            version: Optional[str] = None) -> Optional[str]:
    """Called first thing by ``herald-tunnel run``. Returns "probation" when
    this process is a freshly swapped-in version, "rolled_back" when it found
    that version crash-looping and swapped the previous one back (the caller
    must re-exec), else None."""
    st = load_state(path)
    p = st.get("pending")
    if not p or layout is None:
        return None
    cur = version or current_version()
    if cur != p.get("to_version"):
        # Rolled back, or reinstalled by hand: nothing to judge.
        st.pop("pending", None)
        save_state(st, path)
        return None
    p["boots"] = int(p.get("boots", 0)) + 1
    if p["boots"] > MAX_PROBATION_BOOTS:
        return "rolled_back" if rollback(layout, st, "crash_loop", path=path) else None
    save_state(st, path)
    return "probation"


def exec_restart(layout: Layout) -> None:  # pragma: no cover - replaces the process
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    os.execv(str(layout.bin), [str(layout.bin), "run"])


# ── the in-process updater ───────────────────────────────────────────────────

class BridgeUpdater:
    """Runs inside ``herald-tunnel run`` next to the relay client."""

    def __init__(self, *, relay_url: str, device_token: str, busy: Callable[[], bool],
                 cfg: Optional[dict] = None, layout: Optional[Layout] = None,
                 probation: bool = False, state_file: Optional[Path] = None,
                 restart: Callable[[Layout], None] = exec_restart,
                 fetch: Optional[Callable[[], Awaitable[dict]]] = None,
                 download: Optional[Callable[[dict], Awaitable[Path]]] = None,
                 stager: Callable[..., Path] = stage,
                 relay_reachable: Optional[Callable[[], Awaitable[bool]]] = None,
                 keys: Optional[dict[str, str]] = None, detect: bool = True):
        self.relay_url = relay_url.rstrip("/")
        self.device_token = device_token
        self.busy = busy
        self.cfg = cfg or {}
        self.layout = layout if layout is not None or not detect else Layout.detect()
        self.probation = probation
        self.state_file = state_file
        self._restart = restart
        self._fetch = fetch or self._fetch_manifest
        self._download = download or self._download_wheel
        self._stager = stager
        self._relay_reachable = relay_reachable or self._probe_relay
        self._keys = keys
        self.state = "idle"
        self.target: Optional[str] = None
        self.error: Optional[str] = None
        self.last_check: Optional[float] = None
        self.registered = asyncio.Event()
        self._wake: Optional[asyncio.Event] = None
        self._force = False
        self._running_apply = False
        # The relay hints the latest release on every (re)connect, and streams
        # are recycled every few minutes: act on each hinted version once.
        self._hinted: Optional[str] = None

    # -- what the app and the relay see ---------------------------------------

    def availability(self) -> tuple[bool, str]:
        if opted_out(self.cfg):
            return False, "disabled"
        if self.layout is None:
            return False, "not_managed"
        return True, ""

    def status(self) -> dict:
        ok, reason = self.availability()
        return {
            "version": current_version(),
            "auto_update": ok,
            "reason": reason or None,
            "state": self.state if ok else reason,
            "target": self.target,
            "error": self.error,
            "last_check": self.last_check,
        }

    def request_now(self, *, force: bool = True) -> dict:
        """'Update now' from the app (forced: skips the rollout slice) or a
        hint from the relay (not forced). Returns immediately."""
        ok, _ = self.availability()
        if ok:
            self._force = self._force or force
            if self._wake is not None:
                self._wake.set()
            if not self._running_apply:
                self.state = "checking"
        return self.status()

    def on_registered(self, reply: Optional[dict] = None) -> None:
        """The relay accepted us: the health check a new version must pass."""
        if self.probation:
            self.commit()
        self.registered.set()
        hint = (reply or {}).get("plugin") if isinstance(reply, dict) else None
        latest = hint.get("latest") if isinstance(hint, dict) else None
        try:
            if (isinstance(latest, str) and latest != self._hinted
                    and newer(latest, current_version())):
                self._hinted = latest
                # A hint only: the signed manifest still decides, and the
                # rollout slice still applies.
                self.request_now(force=False)
        except ValueError:
            pass

    def commit(self) -> None:
        st = load_state(self.state_file)
        p = st.pop("pending", None)
        self.probation = False
        if not p:
            return
        st["last_good"] = current_version()
        st["last_result"] = f"updated:{p.get('from_version')}->{p.get('to_version')}"
        st["updated_at"] = time.time()
        save_state(st, self.state_file)
        logger.info("Herald Bridge %s is healthy (was %s)", p.get("to_version"), p.get("from_version"))
        if self.layout is not None:
            keep = {self.layout.root}
            if p.get("from_path"):
                keep.add(Path(p["from_path"]))
            prune(self.layout, keep)

    # -- the loop ---------------------------------------------------------------

    async def run_forever(self) -> None:
        self._wake = asyncio.Event()
        ok, reason = self.availability()
        if not ok:
            logger.info("Herald Bridge auto-update off (%s)", reason)
        watchdog = asyncio.create_task(self._probation_watchdog()) if self.probation else None
        delay = FIRST_CHECK_S + random.uniform(0, 300)
        deadline = time.monotonic() + delay
        try:
            while True:
                try:
                    await asyncio.wait_for(self._wake.wait(),
                                           timeout=max(0.0, min(REQUEST_POLL_S, deadline - time.monotonic())))
                except asyncio.TimeoutError:
                    pass
                if self._take_request_file():
                    self._force = True
                    self._wake.set()
                if not self._wake.is_set() and time.monotonic() < deadline:
                    continue
                self._wake.clear()
                force, self._force = self._force, False
                if self.availability()[0] and not self.probation:
                    await self.check_and_apply(force=force)
                deadline = time.monotonic() + CHECK_EVERY_S + random.uniform(-JITTER_S, JITTER_S)
        finally:
            if watchdog:
                watchdog.cancel()

    def _take_request_file(self) -> bool:
        p = request_path() if self.state_file is None else self.state_file.with_name(
            "bridge-update.request")
        try:
            p.unlink()
            return True
        except OSError:
            return False

    async def check_and_apply(self, *, force: bool = False) -> str:
        """One check. Returns a short result code (also kept in ``state``)."""
        if self._running_apply:
            return "busy"
        self._running_apply = True
        self.error = None
        try:
            return await self._check_and_apply(force)
        except UpdateError as e:
            self.state, self.error = "failed", e.code
            self._record(f"failed:{e.code}")
            logger.warning("Herald Bridge update: %s", e)
            return e.code
        except Exception as e:  # noqa: BLE001
            self.state, self.error = "failed", type(e).__name__
            self._record(f"failed:{type(e).__name__}")
            logger.warning("Herald Bridge update failed: %r", e)
            return "error"
        finally:
            self._running_apply = False

    async def _check_and_apply(self, force: bool) -> str:
        self.state = "checking"
        self.last_check = time.time()
        envelope = await self._fetch()
        m = verify_envelope(envelope, keys=self._keys)
        cur = current_version()
        if not newer(m["version"], cur):
            self.state, self.target = "current", None
            self._record("current")
            return "current"
        bad = load_state(self.state_file).get("bad", {})
        if m["version"] in bad and not force:
            self.state = "current"
            self._record(f"skipped_bad:{m['version']}")
            return "skipped_bad"
        if not force and not in_rollout(self.device_token, m):
            self.state = "current"
            self._record(f"not_in_rollout:{m['version']}")
            return "not_in_rollout"
        assert self.layout is not None
        self.target = m["version"]
        self.state = "downloading"
        wheel = await self._download(m)
        self.state = "installing"
        new_venv = await asyncio.to_thread(self._stager, self.layout, m, wheel)
        self.state = "waiting_idle"
        await self.wait_idle()
        self.state = "restarting"
        previous = swap(self.layout, new_venv, from_version=cur)
        st = load_state(self.state_file)
        st["pending"] = {"from_version": cur, "from_path": str(previous),
                         "to_version": m["version"], "to_path": str(new_venv),
                         "started": time.time(), "boots": 0}
        st["last_result"] = f"swapped:{cur}->{m['version']}"
        save_state(st, self.state_file)
        logger.info("Herald Bridge %s installed (was %s); restarting the tunnel",
                    m["version"], cur)
        self._restart(self.layout)
        return "restarting"

    def _record(self, result: str) -> None:
        try:
            st = load_state(self.state_file)
            st["last_check"] = time.time()
            st["last_result"] = result
            save_state(st, self.state_file)
        except OSError:
            pass

    async def wait_idle(self) -> None:
        """Return once nothing has been in flight for IDLE_FOR_S."""
        quiet_since: Optional[float] = None
        while True:
            now = time.monotonic()
            if self.busy():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = now
            elif now - quiet_since >= IDLE_FOR_S:
                return
            await asyncio.sleep(IDLE_POLL_S)

    async def _probation_watchdog(self) -> None:
        """Roll back a new version that cannot register while the relay is up.
        A relay (or network) outage is not the update's fault: keep waiting."""
        while self.probation:
            try:
                await asyncio.wait_for(self.registered.wait(), timeout=PROBATION_S)
                return
            except asyncio.TimeoutError:
                pass
            if not self.probation:
                return
            if await self._relay_reachable():
                assert self.layout is not None
                if rollback(self.layout, load_state(self.state_file), "no_register",
                            path=self.state_file):
                    self._restart(self.layout)
                return

    # -- network ----------------------------------------------------------------

    async def _fetch_manifest(self) -> dict:
        import httpx  # noqa: PLC0415
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(self.relay_url + MANIFEST_PATH)
        if r.status_code == 404:
            raise UpdateError("no_release")
        if r.status_code != 200:
            raise UpdateError("manifest_http", str(r.status_code))
        try:
            body = r.json()
        except ValueError as e:
            raise UpdateError("bad_manifest", "json") from e
        env = body.get("envelope") if isinstance(body, dict) and "envelope" in body else body
        if not isinstance(env, dict):
            raise UpdateError("bad_manifest", "not an object")
        return env

    async def _probe_relay(self) -> bool:
        import httpx  # noqa: PLC0415
        try:
            async with httpx.AsyncClient(timeout=10.0) as c:
                return (await c.get(self.relay_url + "/health")).status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def _download_wheel(self, m: dict) -> Path:
        import httpx  # noqa: PLC0415
        assert self.layout is not None
        cache = self.layout.versions / ".cache"
        cache.mkdir(parents=True, exist_ok=True)
        dest = cache / m["wheel_name"]
        if dest.exists() and _sha256(dest) == m["sha256"]:
            return dest
        tmp = cache / (m["wheel_name"] + ".part")
        h, n = hashlib.sha256(), 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0),
                                     follow_redirects=True) as c:
            async with c.stream("GET", m["wheel_url"]) as r:
                if r.status_code != 200:
                    raise UpdateError("download_http", str(r.status_code))
                with open(tmp, "wb") as f:
                    async for chunk in r.aiter_bytes():
                        n += len(chunk)
                        if n > MAX_WHEEL_BYTES:
                            f.close()
                            tmp.unlink(missing_ok=True)
                            raise UpdateError("download_too_big")
                        h.update(chunk)
                        f.write(chunk)
        if h.hexdigest() != m["sha256"]:
            tmp.unlink(missing_ok=True)
            raise UpdateError("hash_mismatch")
        os.replace(tmp, dest)
        return dest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()
