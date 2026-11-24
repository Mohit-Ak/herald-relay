"""Move a Herald Bridge that can't update itself into ``~/.herald-relay``.

The self-updater (``bridge_update.py``) only touches a venv Herald owns:
``~/.herald-relay`` (local machines), ``~/.herald/venv`` (Herald Cloud) or
``HERALD_BRIDGE_ROOT``. A bridge someone installed with ``pip install`` into
Hermes' own venv, with ``pip --user``, or into a venv of their own choosing
(``~/herald-tunnel-venv`` on our first machines) runs fine but stays on that
version forever. With a hundred customers nobody can update those by hand.

So the bridge moves itself, once, with no manual step:

1. **Where it runs.** ``herald-tunnel setup`` (before it writes anything) and
   ``herald-tunnel run`` (in the background, after the tunnel is up) check
   whether they run from a venv Herald owns. If not, and the move is allowed
   (see ``eligibility``), they continue below.
2. **Install the owned copy.** The same signed release manifest the updater
   uses names a wheel (sha256-checked). It is installed into
   ``~/.herald-relay-versions/<version>`` and must pass
   ``herald-tunnel selfcheck``; ``~/.herald-relay`` then becomes a symlink to
   it, exactly the shape the updater leaves behind after an update. A working
   ``~/.herald-relay`` that is already there is reused.
3. **Point the launchers at it.** The systemd user unit's ``ExecStart`` (only
   that line; Environment= and the rest are kept), the Hermes cron keepalive
   script and the Herald skills now name ``~/.herald-relay/bin/herald-tunnel``.
4. **Switch.** ``setup`` re-runs itself from the new copy. ``run`` waits until
   nothing is in flight (the same idle gate as an update) and re-execs into the
   new copy; under systemd the PID stays the same, so the unit never notices.

Nothing about the pairing changes: the device token, relay URL and settings
live in ``$HERMES_HOME/config.yaml``, not in the venv, so the phone keeps
working. The old copy is left where it was (it may be Hermes' own venv).

Not moved (reported as the reason instead): automatic updates turned off,
``HERALD_BRIDGE_MIGRATE=0``, a developer checkout (editable install), or a
tunnel for a non-default ``HERMES_HOME`` (a second tunnel on one machine; two
tunnels sharing one updater root could race each other).
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from . import bridge_update as bu

logger = logging.getLogger("herald.migrate")

UNIT_NAME = "herald-tunnel.service"
_OFF = ("0", "false", "no", "off")


# ── may we move? ─────────────────────────────────────────────────────────────

def target_layout(home: Optional[Path] = None) -> bu.Layout:
    explicit = os.environ.get("HERALD_BRIDGE_ROOT", "").strip()
    root = Path(explicit).expanduser() if explicit else (home or Path.home()) / ".herald-relay"
    return bu.Layout(root=root, versions=root.with_name(root.name + "-versions"))


def is_source_checkout() -> bool:
    """A developer's editable install or a bare checkout: never moved."""
    from importlib.metadata import distribution  # noqa: PLC0415
    dist = None
    for name in bu.PACKAGES:  # hermes-herald, or herald-bridge after the rename
        try:
            dist = distribution(name)
            break
        except Exception:  # noqa: BLE001  (PackageNotFoundError)
            continue
    if dist is None:  # a bare checkout
        return True
    try:
        info = json.loads(dist.read_text("direct_url.json") or "{}")
    except ValueError:
        info = {}
    if (info.get("dir_info") or {}).get("editable"):
        return True
    import herald_relay  # noqa: PLC0415
    try:
        here = Path(herald_relay.__file__).resolve()
        site = Path(str(dist.locate_file(""))).resolve()
        return not str(here).startswith(str(site) + os.sep)
    except (OSError, TypeError):
        return False


def eligibility(cfg: Optional[dict] = None, *, hermes_home: Optional[Path] = None,
                home: Optional[Path] = None, managed: Optional[bool] = None,
                source_checkout: Optional[bool] = None) -> tuple[bool, str]:
    """(may move, reason). The reason is short and safe to show."""
    if managed is None:
        managed = bu.Layout.detect() is not None
    if managed:
        return False, "already_managed"
    if bu.opted_out(cfg):
        return False, "disabled"
    if os.environ.get("HERALD_BRIDGE_MIGRATE", "").strip().lower() in _OFF:
        return False, "migrate_disabled"
    if source_checkout is None:
        source_checkout = is_source_checkout()
    if source_checkout:
        return False, "source_checkout"
    home = home or Path.home()
    hh = Path(hermes_home or os.getenv("HERMES_HOME", home / ".hermes")).expanduser()
    if hh.resolve() != (home / ".hermes").resolve():
        return False, "custom_hermes_home"
    return True, ""


# ── install the owned copy ───────────────────────────────────────────────────

def working_version(root: Path, *, runner: bu.Runner = bu._run) -> Optional[str]:
    """The version a ``herald-tunnel`` under *root* reports, or None if it
    is missing or broken."""
    exe = root / "bin" / "herald-tunnel"
    if not os.access(exe, os.X_OK):
        return None
    try:
        r = runner([str(exe), "selfcheck"])
        return json.loads((r.stdout or "").strip().splitlines()[-1]).get("version") \
            if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError, AttributeError):
        return None


def link_root(layout: bu.Layout, target: Path) -> None:
    """Make ``layout.root`` a symlink to *target* with one rename(2). A real
    directory already there (a broken venv) is moved aside, never deleted."""
    root = layout.root
    if root.exists() and not root.is_symlink():
        aside = root.with_name(f"{root.name}.broken-{int(time.time())}")
        os.rename(root, aside)
        logger.warning("moved the broken %s aside to %s", root, aside)
    tmp = root.with_name(root.name + ".herald-swap")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(tmp, root)


async def prepare(layout: bu.Layout, *, fetch: Callable[[], Awaitable[dict]],
                  download: Callable[[dict], Awaitable[Path]],
                  stager: Callable[..., Path] = bu.stage,
                  runner: bu.Runner = bu._run,
                  keys: Optional[dict[str, str]] = None) -> str:
    """Make ``layout.root`` hold a working Herald Bridge. Returns its version.

    Never touches the copy that is running now."""
    import asyncio  # noqa: PLC0415
    have = await asyncio.to_thread(working_version, layout.root, runner=runner)
    if have:
        return have
    if layout.root.is_symlink():
        # A link to a venv that no longer works: drop the link (only the
        # link) so stage() reinstalls that version instead of refusing.
        layout.root.unlink()
    m = bu.verify_envelope(await fetch(), keys=keys)
    wheel = await download(m)
    target = await asyncio.to_thread(stager, layout, m, wheel)
    link_root(layout, target)
    return m["version"]


# ── point the launchers at it ────────────────────────────────────────────────

def current_unit(cgroup: Optional[str] = None) -> Optional[str]:
    """The systemd unit this process runs under (from /proc/self/cgroup)."""
    if cgroup is None:
        try:
            cgroup = Path("/proc/self/cgroup").read_text()
        except OSError:
            return None
    for line in cgroup.splitlines():
        for part in reversed(line.rsplit(":", 1)[-1].split("/")):
            if part.endswith(".service"):
                return part
    return None


_EXEC = re.compile(r"^(\s*ExecStart\s*=\s*)(.*)$")


def _rewrite_exec(value: str, new_bin: str, root: Path) -> Optional[str]:
    """New ExecStart value pointing at *new_bin*, or None when this line is
    not a herald-tunnel we should move (already owned, or something else)."""
    parts = value.split()
    if not parts:
        return None
    first = parts[0].lstrip("-@:+!")
    if Path(first).name == "herald-tunnel":
        rest = parts[1:]
    elif "herald_relay.tunnel_cli" in parts and "-m" in parts:
        rest = parts[parts.index("herald_relay.tunnel_cli") + 1:]
    else:
        return None
    if Path(first).parent == root / "bin":
        return None
    return " ".join([new_bin, *(rest or ["run"])])


def repoint_unit(unit_file: Path, new_bin: str, root: Path) -> bool:
    try:
        text = unit_file.read_text()
    except OSError:
        return False
    out, changed = [], False
    for line in text.splitlines(keepends=True):
        m = _EXEC.match(line.rstrip("\n"))
        if m:
            new = _rewrite_exec(m.group(2), new_bin, root)
            if new is not None:
                line = f"{m.group(1)}{new}\n"
                changed = True
        out.append(line)
    if changed:
        tmp = unit_file.with_suffix(unit_file.suffix + ".tmp")
        tmp.write_text("".join(out))
        os.replace(tmp, unit_file)
    return changed


def repoint_launchers(root: Path, *, hermes_home: Path, unit_dir: Optional[Path] = None,
                      unit: Optional[str] = None, reload: bool = True) -> list[str]:
    """Rewrite what starts the tunnel so it names ``<root>/bin/herald-tunnel``.
    Returns what changed (for the log and the state file)."""
    new_bin = str(root / "bin" / "herald-tunnel")
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    changed: list[str] = []
    for name in dict.fromkeys(n for n in (unit, UNIT_NAME) if n):
        f = unit_dir / name
        if f.is_file() and repoint_unit(f, new_bin, root):
            changed.append(f"unit:{name}")
    if changed and reload:
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False,
                           capture_output=True, timeout=30)
        except (OSError, subprocess.SubprocessError):
            pass
    ka = hermes_home / "scripts" / "herald_tunnel_keepalive.sh"
    if ka.is_file():
        text = ka.read_text()
        new = re.sub(r"(?m)^exec .*? ensure$", f"exec {new_bin} ensure", text)
        if new != text:
            ka.write_text(new)
            changed.append("keepalive")
    try:
        from . import local_skills  # noqa: PLC0415
        if local_skills.install(hermes_home, command=new_bin, all_profiles=True):
            changed.append("skills")
    except OSError:
        pass
    return changed


def record(hermes_home: Path, result: dict) -> None:
    st = bu.load_state(hermes_home / "herald" / "bridge-update.json")
    st["migration"] = dict(result, at=time.time())
    bu.save_state(st, hermes_home / "herald" / "bridge-update.json")


def reexec(root: Path, argv: list[str]) -> None:  # pragma: no cover - replaces the process
    # execv drops whatever is still buffered: a piped stdout would lose the
    # "Moving Herald Bridge…" lines the user (or an agent) is reading.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:  # noqa: BLE001
            pass
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:  # noqa: BLE001
            pass
    exe = str(root / "bin" / "herald-tunnel")
    os.execv(exe, [exe, *argv])


def from_where() -> str:
    """Where the running copy lives, for the record."""
    return sys.prefix
