"""``herald-tunnel`` — connect a self-hosted Hermes to the Herald relay.

This is the ONE command a self-hoster runs on any Linux box::

    pip install hermes-herald
    herald-tunnel setup            # register device, write config, install service
    herald-tunnel run              # or run in the foreground without systemd
    herald-tunnel status           # is the tunnel up, end to end?

``setup`` is idempotent: re-running it keeps the existing device token unless
``--new-token`` is passed, so a reinstall never silently orphans the token that
a phone, kiosk or allowlist already references.

WHY A CONSOLE SCRIPT AND NOT "COPY THE RUNNER SCRIPT"
-----------------------------------------------------
The original deployment was a hand-copied ``herald_tunnel.py`` with a
hard-coded ``sys.path.insert`` to one developer's checkout. That works exactly
once, on one machine. A console entry point installed by pip has no path
assumptions, survives venv moves, and gives systemd a stable ExecStart.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from .relay_client import HeraldRelayClient

logger = logging.getLogger("herald-tunnel")

HERMES_HOME = Path(os.getenv("HERMES_HOME", Path.home() / ".hermes"))
DEFAULT_RELAY = "http://34.173.138.246:8082"

UNIT_NAME = "herald-tunnel.service"
UNIT_TEMPLATE = """\
[Unit]
Description=Herald relay tunnel (outbound SSE to the Herald cloud relay)
Documentation=https://github.com/Mohit-Ak/herald-relay
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
"""


# ── config plumbing ──────────────────────────────────────────────────────────

def _read_yaml_config() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((HERMES_HOME / "config.yaml").read_text()) or {}
        section = cfg.get("herald")
        return section if isinstance(section, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logger.warning("could not read %s/config.yaml", HERMES_HOME, exc_info=True)
        return {}


def _write_yaml_config(relay_url: str, device_token: str) -> None:
    """Update the herald.* block in config.yaml, preserving everything else."""
    import yaml
    path = HERMES_HOME / "config.yaml"
    cfg = {}
    if path.exists():
        cfg = yaml.safe_load(path.read_text()) or {}
    section = cfg.get("herald")
    if not isinstance(section, dict):
        section = {}
    section["relay_url"] = relay_url
    section["device_token"] = device_token
    cfg["herald"] = section
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, default_flow_style=False, sort_keys=True))


def _api_key() -> str:
    env_path = HERMES_HOME / ".env"
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("API_SERVER_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except FileNotFoundError:
        pass
    return ""


def _register_device(relay_url: str, label: str) -> str:
    req = urllib.request.Request(
        relay_url.rstrip("/") + "/push/register",
        data=json.dumps({"fcm_token": label, "platform": "linux"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.load(r)
    token = body.get("device_token", "")
    if not token:
        raise SystemExit(f"relay did not return a device_token: {body}")
    return token


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_setup(args: argparse.Namespace) -> int:
    relay_url = (args.relay_url or _read_yaml_config().get("relay_url")
                 or DEFAULT_RELAY).rstrip("/")

    key = _api_key()
    if not key:
        print(f"ERROR: no API_SERVER_KEY in {HERMES_HOME}/.env — is the Hermes "
              "api_server platform configured on this machine?", file=sys.stderr)
        return 1

    existing = _read_yaml_config().get("device_token", "")
    if existing and not args.new_token:
        token = existing
        print(f"device token: {token} (existing — pass --new-token to replace)")
    else:
        token = _register_device(relay_url, args.label)
        print(f"device token: {token} (registered)")

    _write_yaml_config(relay_url, token)
    print(f"config written: {HERMES_HOME}/config.yaml (herald.relay_url, herald.device_token)")

    if args.no_systemd:
        print("\nRun the tunnel with: herald-tunnel run")
        return 0

    exec_start = _self_exec() + " run"
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT_NAME).write_text(UNIT_TEMPLATE.format(exec_start=exec_start))
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", UNIT_NAME]):
        subprocess.run(cmd, check=False)
    print(f"systemd user unit installed and started: {UNIT_NAME}")
    print("NOTE: user units stop at logout unless lingering is on:\n"
          f"  sudo loginctl enable-linger {os.getenv('USER', '<user>')}")
    print("\nVerify end-to-end with: herald-tunnel status")
    return 0


def _self_exec() -> str:
    """Absolute ExecStart for systemd: the installed console script if it
    exists (survives shell PATH differences), else `python -m`."""
    candidate = Path(sys.executable).parent / "herald-tunnel"
    if candidate.exists():
        return str(candidate)
    return f"{sys.executable} -m herald_relay.tunnel_cli"


def cmd_run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = _read_yaml_config()
    relay_url = cfg.get("relay_url") or os.getenv("HERALD_RELAY_URL", "")
    device_token = cfg.get("device_token") or os.getenv("HERALD_DEVICE_TOKEN", "")
    local_hermes = os.getenv("HERALD_LOCAL_HERMES_URL", "http://127.0.0.1:8642")
    key = _api_key()

    if not relay_url or not device_token:
        logger.error("missing config — run `herald-tunnel setup` first "
                     "(needs herald.relay_url and herald.device_token in "
                     "%s/config.yaml)", HERMES_HOME)
        return 1

    logger.info("starting tunnel relay=%s local_hermes=%s api_key=%s",
                relay_url, local_hermes, "set" if key else "MISSING")
    client = HeraldRelayClient(
        relay_url=relay_url,
        device_token=device_token,
        local_hermes_url=local_hermes,
        hermes_key=key,
    )
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Prove the path end to end: local Hermes, relay, and the tunnel itself."""
    cfg = _read_yaml_config()
    relay_url = (cfg.get("relay_url") or DEFAULT_RELAY).rstrip("/")
    token = cfg.get("device_token", "")
    ok = True

    def check(label: str, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                body = r.read().decode(errors="replace")[:120]
            print(f"  OK    {label}: {body}")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {label}: {type(e).__name__}: {e}")
            return False

    local = os.getenv("HERALD_LOCAL_HERMES_URL", "http://127.0.0.1:8642")
    ok &= check("local Hermes", local + "/health")
    ok &= check("relay", relay_url + "/health")
    if token:
        # THE test: does the relay see THIS device's tunnel as connected?
        ok &= check("tunnel (this device)",
                    f"{relay_url}/hermes/health?device_token={token}")
        print(f"  device-scoped base for remote callers:\n"
              f"    {relay_url}/hermes/d/{token}")
    else:
        print("  FAIL  no device_token configured — run `herald-tunnel setup`")
        ok = False
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="herald-tunnel",
        description="Connect a self-hosted Hermes to the Herald cloud relay.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="register this device and install the service")
    p_setup.add_argument("--relay-url", default="",
                         help=f"relay base URL (default: {DEFAULT_RELAY})")
    p_setup.add_argument("--label", default=os.uname().nodename,
                         help="device label shown in the relay (default: hostname)")
    p_setup.add_argument("--new-token", action="store_true",
                         help="register a fresh device token even if one exists")
    p_setup.add_argument("--no-systemd", action="store_true",
                         help="only write config; do not install the systemd unit")
    p_setup.set_defaults(func=cmd_setup)

    p_run = sub.add_parser("run", help="run the tunnel in the foreground")
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="check local Hermes, relay, and tunnel")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
