"""Push notification registration and FCM delivery – Firestore-backed."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx, os, uuid, time, logging
from typing import Optional
from services.firestore_client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])

RELAY_URL = os.getenv("HERALD_RELAY_URL", "wss://relay.herald.app")
FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "")
FREE_BURSTS = 20


class RegisterRequest(BaseModel):
    device_token: Optional[str] = None  # None = create new
    fcm_token: str
    platform: str = "android"  # android | ios
    plan: str = "byok"  # byok | credits
    hermes_url_hint: Optional[str] = None


class PushSendRequest(BaseModel):
    device_token: str
    message: str
    urgency: str = "low"  # low | high
    metadata: dict = {}


@router.post("/register")
async def register_device(req: RegisterRequest):
    db = get_db()
    token = req.device_token or str(uuid.uuid4())
    now = time.time()
    relay_ws_url = RELAY_URL.replace("https://", "wss://").replace("http://", "ws://")
    relay_url = relay_ws_url + "/relay/connect"

    # Merge with existing doc if re-registering
    ref = db.collection("devices").document(token)
    existing_doc = ref.get()
    existing = existing_doc.to_dict() if existing_doc.exists else {}

    ref.set({
        "device_token": token,
        "fcm_token": req.fcm_token,
        "platform": req.platform,
        "plan": req.plan,
        "relay_url": relay_url,
        "credits": existing.get("credits", FREE_BURSTS),
        "free_bursts_remaining": existing.get("free_bursts_remaining", FREE_BURSTS),
        "registered_at": existing.get("registered_at", now),
        "last_seen": now,
    })
    logger.info(f"Registered device {token[:8]}... platform={req.platform} plan={req.plan}")
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
