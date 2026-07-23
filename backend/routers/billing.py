"""Billing API: BYOK Gemini key storage and credit management – Firestore-backed."""
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from cryptography.fernet import Fernet, InvalidToken
from datetime import datetime, timezone
import os, logging
from services.firestore_client import get_db

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing", tags=["billing"])


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
    db = get_db()
    dev = db.collection("devices").document(req.device_token).get()
    if not dev.exists:
        raise HTTPException(404, "Device not registered")
    # Update device doc with key + plan, and also store in billing collection
    db.collection("devices").document(req.device_token).set(
        {"encrypted_api_key": encrypted, "plan": "byok"},
        merge=True,
    )
    db.collection("billing").document(req.device_token).set({
        "encrypted_key": encrypted,
        "credits": 0,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, merge=True)
    logger.info(f"Stored API key for {req.device_token[:8]}...")
    return {"ok": True}


# GET /billing/credits
@router.get("/credits")
async def get_credits(device_token: str):
    db = get_db()
    dev_doc = db.collection("devices").document(device_token).get()
    if not dev_doc.exists:
        raise HTTPException(404, "Device not registered")
    d = dev_doc.to_dict()
    return {
        "plan": d.get("plan", "byok"),
        "credits_remaining": d.get("credits", 0),
        "free_bursts_remaining": d.get("free_bursts_remaining", 0),
    }


# POST /billing/credits/add
@router.post("/credits/add")
async def add_credits(req: CreditsAddRequest):
    db = get_db()
    dev_ref = db.collection("devices").document(req.device_token)
    dev_doc = dev_ref.get()
    if not dev_doc.exists:
        raise HTTPException(404, "Device not registered")
    credits_to_add = int(req.amount_usd / 5.0 * 100)  # $5 = 100 credits
    current = dev_doc.to_dict().get("credits", 0)
    new_total = current + credits_to_add
    dev_ref.set({"credits": new_total, "plan": "credits"}, merge=True)
    db.collection("billing").document(req.device_token).set(
        {"credits": new_total, "updated_at": datetime.now(timezone.utc).isoformat()},
        merge=True,
    )
    logger.info(f"Added {credits_to_add} credits for {req.device_token[:8]}...")
    return {"ok": True, "credits_added": credits_to_add, "total_credits": new_total}


# GET /billing/key/decrypt (internal, localhost only)
@router.get("/key/decrypt")
async def decrypt_key(device_token: str, request: Request):
    if not request.client or request.client.host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, "Internal endpoint")
    db = get_db()
    # Try billing collection first, fall back to devices
    bill_doc = db.collection("billing").document(device_token).get()
    encrypted = None
    if bill_doc.exists:
        encrypted = bill_doc.to_dict().get("encrypted_key")
    if not encrypted:
        dev_doc = db.collection("devices").document(device_token).get()
        if dev_doc.exists:
            encrypted = dev_doc.to_dict().get("encrypted_api_key")
    if not encrypted:
        raise HTTPException(404, "No API key stored")
    try:
        f = _get_fernet()
        key = f.decrypt(encrypted.encode()).decode()
        return {"gemini_api_key": key}
    except InvalidToken:
        raise HTTPException(500, "Decryption failed")
