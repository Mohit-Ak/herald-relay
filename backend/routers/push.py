"""Push notification registration and FCM delivery."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx, os, uuid, time, logging
from typing import Optional

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/push", tags=["push"])

# TODO: Replace with Firestore in production
_devices: dict = {}  # device_token -> device dict

RELAY_URL = os.getenv("HERALD_RELAY_URL", "wss://relay.herald.app")
FCM_PROJECT_ID = os.getenv("FCM_PROJECT_ID", "")

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
    token = req.device_token or str(uuid.uuid4())
    now = time.time()
    existing = _devices.get(token, {})
    _devices[token] = {
        **existing,
        "device_token": token,
        "fcm_token": req.fcm_token,
        "platform": req.platform,
        "plan": req.plan,
        "credits": existing.get("credits", 20),
        "free_bursts_remaining": existing.get("free_bursts_remaining", 20),
        "created_at": existing.get("created_at", now),
        "last_seen": now,
    }
    relay_ws_url = RELAY_URL.replace("https://", "wss://").replace("http://", "ws://")
    logger.info(f"Registered device {token[:8]}... platform={req.platform} plan={req.plan}")
    return {"device_token": token, "relay_url": relay_ws_url + "/relay/connect"}

@router.post("/send")
async def send_push(req: PushSendRequest):
    d = _devices.get(req.device_token)
    if not d:
        raise HTTPException(404, "Device not registered")
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
