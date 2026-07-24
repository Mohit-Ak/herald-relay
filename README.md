# Herald Relay

**Cloud relay that bridges Herald voice app push notifications to local Hermes instances behind NAT.**

```
Phone (Flutter) ←── push notification ──── Herald Relay (GCE) ←── SSE/POST ──── Hermes (local machine)
```

Herald Relay is the SaaS backend that makes Herald work anywhere. Hermes runs on your local machine. Herald Cloud pushes tasks and events through this relay to your phone, and routes your approvals back.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────┐
│  Flutter (Herald app)                                        │
│  ├─ PushService   — FCM tokens, push notification handling   │
│  ├─ MonitorService — SSE stream for live run updates         │
│  ├─ RelayProvider  — device pairing, health, approval state  │
│  └─ BurstScreen    — live event feed + approval UI           │
└──────────────┬────────────────────────────┬─────────────────┘
               │ push notification (FCM)    │ SSE+POST
               ▼                            ▼
┌──────────────────────────────────────────────────────────────┐
│  Herald Relay (FastAPI, GCE us-central1)                     │
│  ├─ /push/register    — device registration + FCM token      │
│  ├─ /tunnel/connect   — plugin registers its AgentCard       │
│  ├─ /tunnel/events    — SSE: Cloud→Plugin (A2A tasks)        │
│  ├─ /tunnel/update    — POST: Plugin→Cloud (classified events)│
│  ├─ /tunnel/approval  — POST: Flutter approve/deny           │
│  ├─ /tunnel/pending_approvals — GET: offline approval flush  │
│  ├─ /monitor/{token}/{run_id} — SSE: Cloud→Flutter (live)    │
│  └─ /hermes/v1/*      — transparent proxy to local Hermes    │
│                                                              │
│  Persistence: Firestore (eh-voice-ai project)                │
└──────────────┬───────────────────────────────────────────────┘
               │ SSE tunnel (httpx, reconnect with backoff)
               ▼
┌──────────────────────────────────────────────────────────────┐
│  hermes-herald plugin (Python, runs with Hermes)             │
│  ├─ HeraldRelayClient — SSE subscriber + POST update sender  │
│  ├─ EventClassifier   — 5-tier: IGNORE/ACCUM/MILESTONE/Q/DONE│
│  └─ HeraldRelayPlugin — Hermes plugin entrypoint             │
└──────────────────────────────────────────────────────────────┘
```

---

## Event Classifier Tiers

| Signal | Meaning | Flutter action |
|--------|---------|----------------|
| `IGNORE` | Noise (tool internals, retries) | Dropped silently |
| `ACCUMULATE` | Progress text worth showing | Appended to feed |
| `MILESTONE` | Meaningful step complete | Highlighted card in feed |
| `QUESTION` | Hermes needs a decision | Approval sheet (blocks run) |
| `DONE` | Task finished | Done bar with summary |

---

## Approval Flow

```
Hermes emits QUESTION
   → Plugin sends QUESTION update to Relay
   → Relay forwards to Flutter monitor SSE
   → Flutter shows ApprovalSheet (cannot dismiss without deciding)
   → User taps Approve or Deny
   → Flutter POSTs /tunnel/approval
   → Relay delivers to plugin SSE queue (or Firestore if offline)
   → Plugin unblocks _wait_for_approval()
   → If approved: Hermes continues
   → If denied: DONE sent with denial message
```

---

## Setup

### Herald Cloud (GCE)
```bash
# Backend is live at 34.173.138.246
curl https://34.173.138.246/health
# {"status":"ok","service":"herald-relay","connected_devices":0}
```

### Install Hermes plugin
```bash
pip install hermes-herald
hermes config set herald.relay_url https://34.173.138.246
hermes config set herald.device_token <your-token-from-app>
```

### Flutter app
1. Open Herald app → tap **Connect** → **Herald Cloud**
2. Tap **Register Device** — get your device token
3. Copy the install command shown → run on your Hermes machine
4. Tap **Send test ping** to verify end-to-end

---

## Local Development

```bash
# Backend
cd backend
uv run uvicorn main:app --reload --port 8082

# Plugin tests
cd plugin && uv run pytest tests/ -q   # 31 tests

# Backend tests
cd backend && uv run pytest tests/ -q  # 15 tests

# E2E test (full pipeline)
uv run pytest tests/test_e2e.py -v     # 1 test, ~4s
```

---

## Deployment (GCE)

Files are deployed to `/opt/herald-relay/` on `mailmind-rtc` (us-central1-a, project `eh-voice-ai`).

```bash
# Deploy updated router
gcloud compute scp backend/routers/tunnel.py \
  mailmind-rtc:/opt/herald-relay/routers/tunnel.py \
  --zone us-central1-a --project eh-voice-ai

gcloud compute ssh mailmind-rtc --zone us-central1-a --project eh-voice-ai \
  --command "sudo systemctl restart herald-relay"
```

---

## Tests

| Suite | Count | Command |
|-------|-------|---------|
| Backend tunnel endpoints | 15 | `pytest backend/tests/` |
| Plugin event classifier | 31 | `pytest plugin/tests/` |
| E2E full pipeline | 1 | `pytest tests/test_e2e.py` |
| **Total** | **47** | |

---

## Tech Stack

- **Backend:** Python 3.11, FastAPI, Firestore, Firebase Admin SDK, FCM
- **Plugin:** Python 3.11, httpx (SSE), asyncio
- **Mobile:** Flutter, Riverpod, GoRouter, firebase_messaging, flutter_secure_storage
- **Infra:** GCE (us-central1), Nginx, systemd, Artifact Registry
