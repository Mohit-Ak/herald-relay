#!/usr/bin/env python3
"""Publish a Herald Bridge release that every machine installs by itself
(herald#505).

    # 1. build the wheel, attach it to GitHub release plugin-v<version>, sign
    #    a manifest offering it to 10% of machines:
    python scripts/release_plugin.py --rollout 10
    # 2. later, widen it (re-signs the same release, no rebuild):
    python scripts/release_plugin.py --resign --rollout 100
    # 3. Herald tested a Hermes release: offer it to 10% of boxes (no rebuild),
    #    then widen. The block rides along on every later release.
    python scripts/release_plugin.py --resign --hermes-tag v0.23.3 --hermes-rollout 10
    python scripts/release_plugin.py --resign --hermes-rollout 100
    python scripts/release_plugin.py --resign --hermes-off      # stop offering one

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


def _package() -> str:
    """Distribution name from plugin/pyproject.toml. It must be one the
    INSTALLED bridges accept: switch to herald-bridge only once a release
    whose bridge_update.PACKAGES includes it (0.9.0) is at 100%."""
    text = (REPO / "plugin" / "pyproject.toml").read_text()
    m = re.search(r'^name\s*=\s*"([^"]+)"', text, re.M)
    if not m or m.group(1) not in bridge_update.PACKAGES:
        raise SystemExit(f"plugin/pyproject.toml name must be one of {bridge_update.PACKAGES}")
    return m.group(1)


def build_wheel(out_dir: Path) -> Path:
    subprocess.run(["uv", "build", "--wheel", "--out-dir", str(out_dir), str(REPO / "plugin")],
                   check=True)
    wheels = sorted(out_dir.glob(f"{_package().replace('-', '_')}-*-py3-none-any.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one wheel, got {wheels}")
    return wheels[0]


HERMES_REPO = "https://github.com/NousResearch/hermes-agent"


def resolve_hermes_tag(tag: str) -> str:
    """The commit an upstream stable release tag points at (peeled)."""
    from herald_relay import hermes_update  # noqa: PLC0415
    if not hermes_update._STABLE_TAG.match(tag):
        raise SystemExit(f"{tag!r} is not a stable release tag (vX.Y.Z)")
    out = subprocess.run(["git", "ls-remote", HERMES_REPO, f"refs/tags/{tag}",
                          f"refs/tags/{tag}^{{}}"], check=True, capture_output=True,
                         text=True).stdout.split()
    refs = dict(zip(out[1::2], out[0::2]))
    sha = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    if not sha:
        raise SystemExit(f"{tag} not found in {HERMES_REPO}")
    return sha


def hermes_block(prev: dict | None, args) -> dict | None:
    """The Hermes release Herald tested (hermes_update.py), carried forward
    from the previous manifest unless changed here."""
    if args.hermes_off:
        return None
    h = dict(prev or {})
    if args.hermes_tag:
        h = {"tag": args.hermes_tag, "version": args.hermes_tag[1:],
             "git_sha": resolve_hermes_tag(args.hermes_tag),
             "rollout_percent": h.get("rollout_percent", 0) if h.get("tag") == args.hermes_tag
             else 0, "tested_at": int(time.time())}
    if not h:
        return None
    if args.hermes_rollout is not None:
        if not 0 <= args.hermes_rollout <= 100:
            raise SystemExit("--hermes-rollout must be 0..100")
        h["rollout_percent"] = args.hermes_rollout
    return h


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
    p.add_argument("--hermes-tag", default="",
                   help="Hermes stable release Herald tested (vX.Y.Z); boxes update to it")
    p.add_argument("--hermes-rollout", type=int, default=None,
                   help="percent of boxes offered that Hermes release (default: keep / 0)")
    p.add_argument("--hermes-off", action="store_true",
                   help="stop offering a Hermes release")
    args = p.parse_args()
    if not 0 <= args.rollout <= 100:
        raise SystemExit("--rollout must be 0..100")
    now = int(time.time())

    prev_hermes = None
    if OUT.exists():
        try:
            prev_hermes = json.loads(base64.b64decode(json.loads(OUT.read_text())["payload"])) \
                .get("hermes")
        except (ValueError, KeyError):
            pass
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
        m = {"package": _package(), "version": version, "wheel_name": wheel.name,
             "wheel_url": url, "sha256": digest, "git_sha": sha, "git_ref": tag,
             "min_supported": args.min_supported or prev_min,
             "rollout_percent": args.rollout, "released_at": now, "signed_at": now,
             "expires": now + EXPIRES_DAYS * 86400}

    h = hermes_block(prev_hermes, args)
    if h:
        m["hermes"] = h
    else:
        m.pop("hermes", None)

    env = sign_envelope(m, _key(args))
    # Prove the plugin will accept it before anything is written.
    got = bridge_update.verify_envelope(env)
    from herald_relay import hermes_update  # noqa: PLC0415
    pin = hermes_update.pin_from(got)
    if h and pin is None:
        raise SystemExit("the hermes block does not validate; nothing written")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(env, indent=1) + "\n")
    print(f"wrote {OUT.relative_to(REPO)}: {got['version']} to {got['rollout_percent']}% "
          f"(sha256 {got['sha256'][:12]}…, expires in {EXPIRES_DAYS} days)")
    if pin:
        print(f"  Hermes {pin['tag']} ({pin['git_sha'][:12]}) to {pin['rollout_percent']}% of boxes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
