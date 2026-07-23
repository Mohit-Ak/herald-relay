from pydantic import BaseModel
from enum import Enum


class Plan(str, Enum):
    BYOK = "byok"       # bring your own key
    CREDITS = "credits"


class Device(BaseModel):
    device_token: str
    fcm_token: str | None = None
    platform: str = "android"   # android | ios
    plan: Plan = Plan.BYOK
    credits: int = 20            # free tier: 20 bursts
    free_bursts_remaining: int = 20
    encrypted_api_key: str | None = None
    created_at: float
    last_seen: float


class RelaySession(BaseModel):
    relay_id: str
    device_token: str
    connected_at: float
    hermes_version: str | None = None
