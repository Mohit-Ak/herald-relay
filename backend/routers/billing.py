"""Billing API: BYOK Gemini key storage and credit management."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from cryptography.fernet import Fernet, InvalidToken
import os, time, logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])

# TODO: Replace with Firestore in production
_devices: dict = {}  # device_token -> Device dict (shared with push.py via import)

def _get_fernet() -> Fernet:
    key = os.getenv("ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("ENCRYPTION_KEY env var not set")
    return Fernet(key.encode())

class KeyRequest(BaseModel):
    device_token: str
    gemini_api_key: str

class CreditsAddRequest(BaseModel):
    device_token: str
    amount_usd: float

# POST /billing/key - store encrypted Gemini API key
@router.post("/key")
async def store_api_key(req: KeyRequest):
    f = _get_fernet()
    encrypted = f.encrypt(req.gemini_api_key.encode()).decode()
    from routers.push import _devices
    if req.device_token not in _devices:
        raise HTTPException(404, "Device not registered")
    _devices[req.device_token]["encrypted_api_key"] = encrypted
    _devices[req.device_token]["plan"] = "byok"
    logger.info(f"Stored API key for {req.device_token[:8]}...")
    return {"ok": True}

# GET /billing/credits
@router.get("/credits")
async def get_credits(device_token: str):
    from routers.push import _devices
    d = _devices.get(device_token)
    if not d:
        raise HTTPException(404, "Device not registered")
    return {
        "plan": d.get("plan", "byok"),
        "credits_remaining": d.get("credits", 0),
        "free_bursts_remaining": d.get("free_bursts_remaining", 0),
    }

# POST /billing/credits/add
@router.post("/credits/add")
async def add_credits(req: CreditsAddRequest):
    from routers.push import _devices
    if req.device_token not in _devices:
        raise HTTPException(404, "Device not registered")
    credits_to_add = int(req.amount_usd / 5.0 * 100)  # $5 = 100 credits
    _devices[req.device_token]["credits"] = _devices[req.device_token].get("credits", 0) + credits_to_add
    _devices[req.device_token]["plan"] = "credits"
    logger.info(f"Added {credits_to_add} credits for {req.device_token[:8]}...")
    return {"ok": True, "credits_added": credits_to_add, "total_credits": _devices[req.device_token]["credits"]}

# GET /billing/key/decrypt (internal, localhost only)
@router.get("/key/decrypt")
async def decrypt_key(device_token: str, request: Request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Internal endpoint")
    from routers.push import _devices
    d = _devices.get(device_token)
    if not d or not d.get("encrypted_api_key"):
        raise HTTPException(404, "No API key stored")
    try:
        f = _get_fernet()
        key = f.decrypt(d["encrypted_api_key"].encode()).decode()
        return {"gemini_api_key": key}
    except InvalidToken:
        raise HTTPException(500, "Decryption failed")
