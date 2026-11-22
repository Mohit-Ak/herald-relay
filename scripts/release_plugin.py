#!/usr/bin/env python3
"""Publish a Herald Bridge release that every machine installs by itself
(herald#505).

    # 1. build the wheel, attach it to GitHub release plugin-v<version>, sign
    #    a manifest offering it to 10% of machines:
    python scripts/release_plugin.py --rollout 10
    # 2. later, widen it (re-signs the same release, no rebuild):
    python scripts/release_plugin.py --resign --rollout 100

Both write backend/release/plugin-manifest.json. Commit it in a PR; the relay
serves it at GET /relay/plugin/manifest once deployed. Nothing reaches a
machine before that deploy.

Signing happens HERE, on the releaser's machine, never on the relay: the
private key (Secret Manager ``herald-relay-signing-key``, Ed25519 PKCS#8 PEM)
is read into memory with gcloud and never written to disk or printed. A
compromised relay can therefore withhold updates but cannot ship code. The
public half is compiled into the plugin (bridge_update.TRUSTED_KEYS).

Envelope format: DSSE (https://github.com/secure-systems-lab/dsse) with
payloadType ``application/vnd.herald.bridge-manifest+json``.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "plugin"))
from herald_relay import bridge_update  # noqa: E402

OUT = REPO / "backend" / "release" / "plugin-manifest.json"
GH_REPO = "Mohit-Ak/herald-relay"
PROJECT = os.environ.get("HERALD_GCP_PROJECT", "eh-voice-ai")
SECRET = "herald-relay-signing-key"
KEY_ID = "herald-bridge-2026-11"
EXPIRES_DAYS = 365  # TUF: targets metadata expire about yearly


def sign_envelope(manifest: dict, private_pem: bytes, key_id: str = KEY_ID) -> dict:
    from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
    key = serialization.load_pem_private_key(private_pem, password=None)
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    sig = key.sign(bridge_update.pae(bridge_update.PAYLOAD_TYPE, body))  # type: ignore[call-arg]
    return {"payloadType": bridge_update.PAYLOAD_TYPE,
            "payload": base64.b64encode(body).decode(),
            "signatures": [{"keyid": key_id, "sig": base64.b64encode(sig).decode()}]}


def _key(args) -> bytes:
    if args.key_file:
        return Path(args.key_file).read_bytes()
    return subprocess.run(
        ["gcloud", "secrets", "versions", "access", "latest", "--secret", SECRET,
         "--project", PROJECT], check=True, capture_output=True).stdout


def _git(*a: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *a], check=True, capture_output=True,
                          text=True).stdout.strip()


def _version() -> str:
    text = (REPO / "plugin" / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise SystemExit("no version in plugin/pyproject.toml")
    return m.group(1)


def build_wheel(out_dir: Path) -> Path:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out_dir), str(REPO / "plugin")],
                   check=True)
    wheels = sorted(out_dir.glob("hermes_herald-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, got {wheels}")
    return wheels[0]


def main() -> int:
    p = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    p.add_argument("--rollout", type=int, default=10, help="percent of machines (0-100)")
    p.add_argument("--min-supported", default="",
                   help="oldest version the app still fully supports (default: keep / 0.7.0)")
    p.add_argument("--resign", action="store_true",
                   help="re-sign the current manifest (e.g. new --rollout), no rebuild")
    p.add_argument("--no-upload", action="store_true",
                   help="build + sign only; do not create the GitHub release")
    p.add_argument("--key-file", default="", help="PEM key file instead of Secret Manager")
    args = p.parse_args()
    if not 0 <= args.rollout <= 100:
        raise SystemExit("--rollout must be 0..100")
    now = int(time.time())

    if args.resign:
        old = json.loads(OUT.read_text())
        m = json.loads(base64.b64decode(old["payload"]))
        m["rollout_percent"] = args.rollout
        if args.min_supported:
            m["min_supported"] = args.min_supported
        m["signed_at"] = now
        m["expires"] = now + EXPIRES_DAYS * 86400
    else:
        if _git("status", "--porcelain", "--", "plugin"):
            raise SystemExit("plugin/ has uncommitted changes: release a commit, not a worktree")
        sha = _git("rev-parse", "HEAD")
        version = _version()
        tag = f"plugin-v{version}"
        with tempfile.TemporaryDirectory() as d:
            wheel = build_wheel(Path(d))
            digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
            url = f"https://github.com/{GH_REPO}/releases/download/{tag}/{wheel.name}"
            if not args.no_upload:
                subprocess.run(["gh", "release", "create", tag, str(wheel), "--repo", GH_REPO,
                                "--target", sha, "--title", f"Herald Bridge {version}",
                                "--notes", f"Herald Bridge {version}\n\nsha256 {digest}"],
                               check=True)
        prev_min = "0.7.0"
        if OUT.exists():
            try:
                prev_min = json.loads(base64.b64decode(json.loads(OUT.read_text())["payload"])) \
                    .get("min_supported", prev_min)
            except (ValueError, KeyError):
                pass
        m = {"package": bridge_update.PACKAGE, "version": version, "wheel_name": wheel.name,
             "wheel_url": url, "sha256": digest, "git_sha": sha, "git_ref": tag,
             "min_supported": args.min_supported or prev_min,
             "rollout_percent": args.rollout, "released_at": now, "signed_at": now,
             "expires": now + EXPIRES_DAYS * 86400}

    env = sign_envelope(m, _key(args))
    # Prove the plugin will accept it before anything is written.
    got = bridge_update.verify_envelope(env)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(env, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(REPO)}: {got['version']} to {got['rollout_percent']}% "
          f"(sha256 {got['sha256'][:12]}…, expires in {EXPIRES_DAYS} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
