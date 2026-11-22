"""Herald Bridge releases and versions (herald#505).

Mounted under /relay (main.py):

  GET  /relay/plugin/manifest   public. The signed release manifest (a DSSE
                                envelope made OFFLINE by scripts/release_plugin.py)
                                plus unsigned conveniences: latest, min_supported,
                                min_plugin_version. The relay never signs anything:
                                a compromised relay can withhold an update but
                                cannot make a machine install one.
  POST /relay/plugin/update-now ops only. Tells one connected machine (or all)
                                to check for an update now. Only a trigger: the
                                machine still verifies the signed manifest.
  GET  /relay/plugin/versions   ops only. Machines per Herald Bridge version,
                                from the plugin_version each one reports on
                                POST /tunnel/connect.

"Ops only" = header ``X-Herald-Ops-Token`` equal to HERALD_RELAY_OPS_TOKEN.
Without that env var both answer 404, so nothing new is exposed until an
operator turns it on.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from services import firestore_client

logger = logging.getLogger(__name__)
router = APIRouter(tags=["plugin"])

_DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "release" / "plugin-manifest.json"
ACTIVE_DAYS = 30
_cache: dict = {"mtime": None, "path": None, "value": None}


def _manifest_path() -> Path:
    return Path(os.getenv("HERALD_PLUGIN_MANIFEST", "") or _DEFAULT_MANIFEST)


def load_release() -> Optional[dict]:
    """{"envelope": ..., "manifest": <unverified payload>} or None."""
    path = _manifest_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _cache["path"] == str(path) and _cache["mtime"] == mtime:
        return _cache["value"]
    try:
        env = json.loads(path.read_text())
        m = json.loads(base64.b64decode(env["payload"]))
        value = {"envelope": env, "manifest": m if isinstance(m, dict) else {}}
    except Exception as e:  # noqa: BLE001
        logger.warning("[plugin] unreadable manifest %s: %s", path, type(e).__name__)
        value = None
    _cache.update(path=str(path), mtime=mtime, value=value)
    return value


def min_plugin_version(manifest: Optional[dict] = None) -> str:
    if os.getenv("MIN_PLUGIN_VERSION"):
        return os.environ["MIN_PLUGIN_VERSION"]
    if manifest is None:
        rel = load_release()
        manifest = rel["manifest"] if rel else {}
    return str(manifest.get("min_supported") or "0.0.0")


def hint() -> Optional[dict]:
    """What /tunnel/connect tells a machine about the current release."""
    rel = load_release()
    if not rel:
        return None
    m = rel["manifest"]
    return {"latest": m.get("version"), "min_supported": min_plugin_version(m)}


def _require_ops(token: Optional[str]) -> None:
    want = os.getenv("HERALD_RELAY_OPS_TOKEN", "")
    if not want:
        raise HTTPException(status_code=404, detail="Not Found")
    if not token or not hmac.compare_digest(token, want):
        raise HTTPException(status_code=401, detail={"error": "ops_token_required"})


@router.get("/plugin/manifest", summary="Signed Herald Bridge release manifest")
async def plugin_manifest():
    rel = load_release()
    if not rel:
        raise HTTPException(status_code=404, detail={"error": "no_release"})
    m = rel["manifest"]
    return JSONResponse(
        {"envelope": rel["envelope"], "latest": m.get("version"),
         "min_supported": m.get("min_supported"), "min_plugin_version": min_plugin_version(m)},
        headers={"Cache-Control": "public, max-age=300"},
    )


class UpdateNowRequest(BaseModel):
    device_token: Optional[str] = None
    all: bool = False


@router.post("/plugin/update-now", summary="Ask machines to check for an update now (ops)")
async def plugin_update_now(req: UpdateNowRequest,
                            x_herald_ops_token: Optional[str] = Header(None)):
    _require_ops(x_herald_ops_token)
    from routers import tunnel  # noqa: PLC0415
    from services import edge_client  # noqa: PLC0415
    if not req.all and not req.device_token:
        raise HTTPException(status_code=400, detail={"error": "device_token_or_all"})
    tokens = list(tunnel._plugin_queues) if req.all else [req.device_token]
    frame = {"type": "bridge_update", "force": True}
    sse = edge = 0
    for t in tokens:
        q = tunnel._plugin_queues.get(t)
        if q is not None:
            await q.put(frame)
            sse += 1
        elif not req.all and await edge_client.deliver(t, frame):
            edge += 1
    logger.info("[plugin] update-now sent sse=%d edge=%d all=%s", sse, edge, req.all)
    return {"ok": True, "sent": sse + edge, "sse": sse, "edge": edge}


@router.get("/plugin/versions", summary="Machines per Herald Bridge version (ops)")
async def plugin_versions(x_herald_ops_token: Optional[str] = Header(None)):
    _require_ops(x_herald_ops_token)
    return count_versions()


def count_versions(now: Optional[float] = None) -> dict:
    """Machines seen in the last ACTIVE_DAYS, per reported plugin version.
    Machines that never reported one run a build older than 0.8.0."""
    now = time.time() if now is None else now
    cutoff = now - ACTIVE_DAYS * 86400
    counts: dict[str, int] = {}
    total = 0
    for doc in firestore_client.get_db().collection("devices").stream():
        d = doc.to_dict() or {}
        if float(d.get("last_seen") or 0) < cutoff or "agent_card" not in d:
            continue  # phones register here too; only tunnels send agent_card
        v = str(d.get("plugin_version") or "unknown (<0.8.0)")
        counts[v] = counts.get(v, 0) + 1
        total += 1
    rel = load_release()
    return {"total": total, "window_days": ACTIVE_DAYS,
            "latest": rel["manifest"].get("version") if rel else None,
            "rollout_percent": rel["manifest"].get("rollout_percent") if rel else None,
            "versions": dict(sorted(counts.items(), key=lambda kv: -kv[1]))}
