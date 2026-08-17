"""Push notification registration and FCM delivery – Firestore-backed."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
import httpx, os, uuid, time, logging
from typing import Optional
from services.firestore_client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])

RELAY_URL = os.getenv("HERALD_RELAY_URL", "wss://relay.herald.app")
FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "")
FREE_BURSTS = 20

# A real FCM registration token is a long opaque string (typically 140-180+
# chars). Device records in production accumulated junk in this field —
# "android", a bare uuid, None — from manual curls and from re-registrations
# that posted no token. Pushing to those returns 404 from FCM and, worse, they
# overwrote tokens that actually worked. 100 is a deliberately loose floor:
# comfortably below any genuine token, far above every placeholder seen.
_MIN_FCM_TOKEN_LEN = 100


def is_valid_fcm_token(token: Optional[str]) -> bool:
    """True if *token* is plausibly a real FCM registration token.

    Used to decide whether an incoming token may replace a stored one, and to
    skip devices that can never receive a push.
    """
    return bool(token) and len(token.strip()) >= _MIN_FCM_TOKEN_LEN


class RegisterRequest(BaseModel):
    device_token: Optional[str] = None  # None = create new
    # OPTIONAL on purpose. The Flutter connect screen registers BEFORE it has
    # an FCM token (and self-hosted users may never enable push at all), so
    # requiring it here 422'd every pairing attempt with an error the app
    # surfaced as a bare "HTTP 422". Push delivery already handles a missing
    # token at /push/send (400 "No FCM token on file"), and the client can
    # re-register later to attach one.
    fcm_token: Optional[str] = None
    platform: str = "android"  # android | ios
    plan: str = "byok"  # byok | credits | self_hosted | cloud
    hermes_url_hint: Optional[str] = None


class PushSendRequest(BaseModel):
    device_token: str
    message: str
    urgency: str = "low"  # low | high
    metadata: dict = {}


def _relay_base(request: Request) -> str:
    """Public base URL for this relay, as the CLIENT should dial it.

    Resolution order:
      1. ``HERALD_RELAY_URL`` when it names a real, externally reachable host
         (not localhost/127.0.0.1 and not the unregistered placeholder domain).
      2. The Host header of the incoming request — the address the client
         demonstrably just reached us on, honouring X-Forwarded-Proto/Host
         when behind a proxy.
      3. The configured value as a last resort.
    """
    configured = (os.getenv("HERALD_RELAY_URL", "") or "").strip().rstrip("/")
    bad_hosts = ("localhost", "127.0.0.1", "0.0.0.0", "relay.herald.app")
    if configured and not any(h in configured for h in bad_hosts):
        return configured

    fwd_host = request.headers.get("x-forwarded-host")
    host = fwd_host or request.headers.get("host")
    if host:
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme or "http"
        return f"{proto}://{host}".rstrip("/")
    return configured or "http://localhost:8082"


@router.post("/register")
async def register_device(req: RegisterRequest, request: Request):
    db = get_db()
    token = req.device_token or str(uuid.uuid4())
    now = time.time()
    # Build the ws:// URL the DEVICE will dial back on. HERALD_RELAY_URL is
    # often left at a loopback/placeholder value on the box (it was
    # "http://localhost:8082", which tells a phone to connect to ITSELF, and
    # the packaged default "wss://relay.herald.app" is an unregistered
    # domain). Prefer the host the client actually reached us on, so
    # registration is correct no matter how the box is configured.
    base = _relay_base(request)
    relay_ws_url = base.replace("https://", "wss://").replace("http://", "ws://")
    relay_url = relay_ws_url + "/relay/connect"

    # Merge with existing doc if re-registering
    ref = db.collection("devices").document(token)
    existing_doc = ref.get()
    existing = existing_doc.to_dict() if existing_doc.exists else {}

    # NEVER clobber a good FCM token with a missing or junk one.
    #
    # `fcm_token` is optional (the Flutter connect screen posts only
    # {platform, plan}), and this used an unconditional set(), so a plain
    # re-registration wrote fcm_token=None straight over a working token and
    # silently disabled background push for that device forever. Placeholder
    # values from tests/manual curl ("android", a bare uuid) did the same.
    # Real FCM registration tokens are long (>100 chars); anything shorter is
    # not a token we can push to, so it must never displace one that is.
    incoming = (req.fcm_token or "").strip()
    fcm_token = incoming if is_valid_fcm_token(incoming) else existing.get("fcm_token")
    if incoming and not is_valid_fcm_token(incoming):
        logger.warning(
            "Ignoring implausible fcm_token (len=%d) for %s... — keeping existing",
            len(incoming), token[:8],
        )

    ref.set({
        "device_token": token,
        "fcm_token": fcm_token,
        "platform": req.platform,
        "plan": req.plan,
        "relay_url": relay_url,
        "credits": existing.get("credits", FREE_BURSTS),
        "free_bursts_remaining": existing.get("free_bursts_remaining", FREE_BURSTS),
        "registered_at": existing.get("registered_at", now),
        "last_seen": now,
    })
    logger.info(
        f"Registered device {token[:8]}... platform={req.platform} "
        f"plan={req.plan} push={'yes' if fcm_token else 'NO'}"
    )
    return {"device_token": token, "relay_url": relay_url}


@router.post("/send")
async def send_push(req: PushSendRequest):
    db = get_db()
    doc = db.collection("devices").document(req.device_token).get()
    if not doc.exists:
        raise HTTPException(404, "Device not registered")
    d = doc.to_dict()
    fcm_token = d.get("fcm_token")
    if not fcm_token:
        raise HTTPException(400, "No FCM token on file")

    if not FCM_PROJECT_ID:
        logger.info(f"[FCM STUB] Push to {req.device_token[:8]}... urgency={req.urgency}: {req.message}")
        return {"ok": True, "stub": True}

    # Real FCM via Google credentials
    try:
        import google.auth
        import google.auth.transport.requests
        credentials, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/firebase.messaging"])
        credentials.refresh(google.auth.transport.requests.Request())
        access_token = credentials.token
    except Exception as e:
        logger.error(f"Failed to get FCM credentials: {e}")
        raise HTTPException(500, "FCM auth failed")

    payload = {
        "message": {
            "token": fcm_token,
            "notification": {"title": "Hermes", "body": req.message},
            "data": {
                "type": "herald_burst",
                "urgency": req.urgency,
                "relay_id": req.metadata.get("relay_id", ""),
                **{k: str(v) for k, v in req.metadata.items()},
            },
            "android": {"priority": "high" if req.urgency == "high" else "normal"},
            "apns": {"headers": {"apns-priority": "10" if req.urgency == "high" else "5"}},
        }
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"https://fcm.googleapis.com/v1/projects/{FCM_PROJECT_ID}/messages:send",
            headers={"Authorization": f"Bearer {access_token}"},
            json=payload,
            timeout=10.0,
        )
        resp.raise_for_status()
    logger.info(f"Push sent to {req.device_token[:8]}... urgency={req.urgency}")
    return {"ok": True}
