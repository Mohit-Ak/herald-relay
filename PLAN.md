# Herald Relay — Implementation Plan

> **Product vision:** Hermes works in the background. Herald is how you talk to it — push-notified burst voice conversations, async by default, no persistent call.

---

## What we're building

### 1. `herald-relay` — New standalone repo
A cloud relay service that:
- Accepts persistent outbound WebSocket connections from local Hermes instances (via a plugin)
- Routes push notifications to mobile clients via FCM/APNs
- Provisions billing (Gemini API key passthrough OR credit purchase)
- Has a marketing website explaining how to install the plugin

### 2. `hermes-herald-plugin` — Hermes plugin
Installed by the user in their local Hermes. Dials out to Herald Relay on startup, maintains a persistent WS connection. Routes Hermes runs/events through to Herald cloud.

### 3. Flutter app changes
- Remove always-on WebRTC model
- **Idle mode:** app in background, waiting for push
- **Burst mode:** notification arrives → user taps → WebRTC spins up → burst conversation → tears down
- **Async transcript pane:** see what Hermes has been doing while you were away

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Herald Cloud (GCE/Cloud Run)              │
│                                                                   │
│  ┌──────────────┐   ┌─────────────────┐   ┌──────────────────┐  │
│  │ Relay WS     │   │  Push Gateway   │   │  Billing API     │  │
│  │ /connect     │   │  FCM + APNs     │   │  /credits        │  │
│  │ (per-device  │   │                 │   │  Stripe + Gemini │  │
│  │  token auth) │   └────────┬────────┘   └──────────────────┘  │
│  └──────┬───────┘            │                                   │
│         │                    │ push                              │
└─────────┼────────────────────┼───────────────────────────────────┘
          │ persistent WS       │
          │ (outbound from      │ FCM/APNs
          │  local Hermes)      ▼
          │              ┌─────────────────┐
          │              │  Flutter App    │
          │              │  (background)   │
          │              │                 │
          └──────────────┤  Tap notify →   │
       burst WebRTC      │  WebRTC burst   │
       (only during      │  30s session    │
        conversation)    └─────────────────┘
          ▲
          │
┌─────────┴────────────────────────────────┐
│  User's local machine                     │
│                                           │
│  Hermes ──► herald-plugin ──► WS relay   │
│  (working on long tasks)                  │
│  - fires push when it needs user input    │
│  - or when a task completes               │
└───────────────────────────────────────────┘
```

---

## Cost model (why this is efficient)

| State | What's running | Cost |
|---|---|---|
| **Hermes working** | Idle WS relay (~1KB/s keepalive) | ~$0.000001/min |
| **Notification** | FCM push (free) | $0 |
| **Burst conversation** | WebRTC session (Pion sidecar, STT, TTS, Hermes API) | ~$0.05-0.15 per burst |
| **Always-on call (old model)** | Full WebRTC + STT stream | ~$0.50-2/hour |

**Key insight:** 95% of the time, the relay is just a WebSocket keepalive. We only spin up the expensive media plane for the 30-60 seconds the user is actually speaking.

---

## Billing model

```
Option A: Bring Your Own Key
  User provides their Gemini API key (stored encrypted in Hermes plugin config)
  Herald charges only a small relay/infra fee (~$2/month flat)
  → Best for technical users, power users

Option B: Buy Credits from Herald
  No API key needed
  Credits = compute minutes (STT + TTS + Gemini calls)
  Pricing: ~$5 for ~100 burst conversations (≈ 5¢ each)
  Free tier: 20 free bursts on signup
  → Best for mainstream users
```

Billing endpoint accepts both. The relay itself doesn't care — it just routes.

---

## Repo structure: `herald-relay` (new standalone)

```
herald-relay/
├── backend/                  # FastAPI relay service
│   ├── main.py
│   ├── routers/
│   │   ├── relay.py          # WS /connect endpoint (Hermes plugin dials in)
│   │   ├── push.py           # Push notification delivery
│   │   ├── billing.py        # Credits + API key management
│   │   └── rtc.py            # WebRTC burst session (reused from Herald)
│   ├── services/
│   │   ├── relay_manager.py  # Active connection registry (device_token → WS)
│   │   ├── push_gateway.py   # FCM + APNs
│   │   ├── billing_service.py
│   │   └── hermes_proxy.py   # Routes requests through relay WS to local Hermes
│   └── models/
│       ├── device.py
│       └── relay_session.py
├── plugin/                   # Hermes plugin (Python package)
│   ├── herald_relay/
│   │   ├── __init__.py
│   │   ├── plugin.py         # Hermes plugin entry point
│   │   ├── relay_client.py   # Outbound WS to Herald Cloud
│   │   └── push_triggers.py  # Decides when to push user
│   └── pyproject.toml
├── website/                  # Docusaurus marketing site
│   ├── docs/
│   │   ├── quickstart.md     # "Connect Herald to your Hermes in 2 minutes"
│   │   ├── billing.md
│   │   └── plugin-config.md
│   └── src/pages/index.tsx
├── infra/
│   └── deploy.yml
└── README.md
```

---

## Phase 1 — Relay + Plugin (Week 1)

### Task 1: Relay WebSocket endpoint
**File:** `backend/routers/relay.py`

The Hermes plugin connects here on startup. One persistent WS per registered Hermes instance. Herald cloud treats this as a tunnel.

```python
# WS protocol:
# Client → Server: {"type": "register", "device_token": "...", "hermes_version": "..."}
# Server → Client: {"type": "registered", "relay_id": "uuid"}
# Server → Client: {"type": "forward_request", "request_id": "...", "method": "POST",
#                    "path": "/v1/runs", "body": {...}}
# Client → Server: {"type": "forward_response", "request_id": "...", "status": 200, "body": {...}}
# Client → Server: {"type": "sse_chunk", "request_id": "...", "data": "..."}
# Client → Server: {"type": "push_trigger", "message": "...", "urgency": "low|high"}
```

### Task 2: Relay manager (in-memory registry)
**File:** `backend/services/relay_manager.py`

```python
class RelayManager:
    _connections: dict[str, WebSocket]  # device_token → ws
    
    async def register(device_token: str, ws: WebSocket)
    async def forward_request(device_token: str, method, path, body) -> Response
    async def send_push_trigger(device_token: str, message: str, urgency: str)
    async def is_connected(device_token: str) -> bool
```

### Task 3: Hermes proxy (routes HTTP through WS tunnel)
**File:** `backend/services/hermes_proxy.py`

Flutter app calls Herald Cloud as if it were Hermes. Herald Cloud tunnels the request through the active WS connection to the local Hermes instance. Response streams back via SSE chunks.

### Task 4: Hermes plugin — relay client
**File:** `plugin/herald_relay/relay_client.py`

```python
class HeraldRelayClient:
    async def connect(relay_url: str, device_token: str)
    async def _handle_forward_request(msg: dict)  # calls local Hermes HTTP
    async def send_push_trigger(message: str, urgency: str)
    async def _keepalive_loop()  # ping every 30s
```

### Task 5: Push trigger logic
**File:** `plugin/herald_relay/push_triggers.py`

When does Hermes push the user?
- `approval_required` SSE event → **high urgency push immediately**
- Task `done` with non-trivial output → **low urgency push**  
- Hermes is `idle` for >30s during a run the user started → checkpoint push
- User-configurable: "always push on done", "only push on approval"

---

## Phase 2 — Flutter burst model (Week 1-2)

### Task 6: App state machine
```
IDLE          — app backgrounded, relay connected, waiting for push
NOTIFIED      — push received, notification shown
BURST_ACTIVE  — user tapped, WebRTC session alive (30-60s typical)
BURST_ENDING  — silence detected or user taps done, 5s countdown
BACKGROUND    — burst ended, transcript saved, back to IDLE
```

### Task 7: Push notification handler
- FCM for Android, APNs for iOS
- Notification payload: `{"type": "herald_burst", "message": "...", "relay_id": "...", "urgency": "high"}`
- High urgency: full-screen notification, ringtone
- Low urgency: silent banner ("Hermes finished your task")

### Task 8: Burst session lifecycle
Replace always-on WebRTC with:
1. User taps notification → WebRTC offer sent to Herald GCE sidecar
2. Herald GCE dials Hermes proxy → proxied to local Hermes via relay WS
3. Voice burst happens (user speaks, Hermes replies)
4. 5s silence → "burst complete" — WebRTC tears down, relay WS persists
5. Transcript appended to async conversation pane

### Task 9: Async conversation pane
Even while WebRTC is torn down, user can see:
- What Hermes has been doing (SSE events streamed through relay)
- Tool calls, checkpoints, results as a timeline
- Tap any item to "reply to this" → starts a burst

---

## Phase 3 — Billing (Week 2)

### Task 10: Device registration + billing
```
POST /api/register
  body: { fcm_token, platform, plan: "byok" | "credits" }
  → { device_token, relay_url }

POST /api/billing/key
  body: { device_token, gemini_api_key }  # encrypted at rest

POST /api/billing/credits/purchase
  body: { device_token, amount_usd }  # Stripe
```

### Task 11: Credit metering
Track per-burst:
- STT duration (seconds)
- TTS characters
- Gemini API calls (tokens in/out)

Each burst debits credits. BYOK users: relay fee only (~flat $2/month).

---

## Phase 4 — Website (Week 2)

Single marketing page + docs. Key messages:
1. "Talk to your AI agent anywhere, anytime"
2. "Herald notifies you when Hermes needs you"
3. "Install the plugin in 2 commands"
4. "Bring your own Gemini key or buy credits"

```bash
# The 2-command install we want in the hero section:
hermes plugins install herald-relay
hermes config set herald.relay_url https://relay.herald.app
hermes config set herald.device_token YOUR_TOKEN_FROM_APP
```

---

## Phase 5 — Repo + brand (Day 1)

- New GitHub repo: `Mohit-Ak/herald-relay` (separate from the mailmind repo)
- Herald app stays in `mailmind` repo for now (rename the GitHub repo to `herald` per the migration doc)
- Plugin published to PyPI as `hermes-herald`
- Website: `herald.app` or similar domain

---

## What stays in the `mailmind` (→ `herald`) repo

- Flutter app (updated for burst model)
- Pion Go sidecar (reused — WebRTC media plane doesn't change)
- Herald backend → **becomes thin relay proxy** (most logic moves to `herald-relay`)

The GCE `mailmind-rtc` box runs:
1. The Pion sidecar (WebRTC media plane — burst sessions)
2. The relay backend (replaces the current Herald backend)

---

## Key decisions (product owner lens)

| Decision | Choice | Why |
|---|---|---|
| Relay persistence | WebSocket (not polling) | Low latency push triggers; works through all NATs |
| Burst trigger | FCM/APNs push | User doesn't need app open — native OS notification |
| Session duration | Max 5 min, idle timeout 30s | Cost control; encourages focused bursts |
| BYOK pricing | $2/month relay fee | Makes power users profitable without API markup |
| Credit pricing | $5 = 100 bursts | ~$0.05/burst; competitive with voice AI apps |
| Free tier | 20 bursts free | Enough to validate value, not enough to abuse |
| Auth | Device token (no Google/Apple login required) | Frictionless — scan QR in app, paste token in plugin |

---

## Non-goals (explicitly out of scope for v1)

- iOS app (Android + web first)
- Multi-device sync (one Hermes instance per device token)
- Transcript storage in the cloud (stays local in the app)
- Team/shared Hermes instances
