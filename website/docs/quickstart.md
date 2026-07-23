---
id: quickstart
title: Quick Start
sidebar_position: 1
---

# Quick Start

Get Herald up and running in under five minutes. By the end of this guide your phone will receive a push notification whenever your Hermes AI agent finishes a task — and you'll be able to reply by voice, right from your lock screen.

---

## Step 1 — Download the Herald app and get your device token

Herald uses a per-device token to route push notifications to exactly your phone. No account required.

1. Download **Herald** from the App Store or Google Play.
2. Open the app and tap **Link Device**.
3. Your unique device token is displayed on screen. Copy it — you'll need it in Step 3.

> **Security note:** your device token is a random 32-byte identifier. It is never stored on Herald servers in plaintext; it is hashed before being associated with a relay endpoint.

---

## Step 2 — Install the Hermes Herald plugin

Herald ships as a standard Hermes plugin via PyPI.

```bash
pip install hermes-herald
```

Verify the install succeeded:

```bash
pip show hermes-herald
# Name: hermes-herald
# Version: 0.x.x
```

---

## Step 3 — Add Herald to your Hermes config

Open `~/.hermes/config.yaml` in your editor and add the following block under `plugins:`. Create the file if it doesn't exist.

```yaml
# ~/.hermes/config.yaml

plugins:
  herald-relay:
    enabled: true
    device_token: "paste-your-token-here"   # from the Herald app (Step 1)
    mode: credits                            # 'credits' (default) or 'byok'

    # Only required when mode: byok
    # gemini_api_key: "AIzaSy..."
```

**Config fields at a glance:**

| Field | Required | Default | Description |
|---|---|---|---|
| `enabled` | No | `true` | Enable or disable the plugin |
| `device_token` | **Yes** | — | Token from the Herald mobile app |
| `mode` | No | `credits` | `credits` = Herald-managed API; `byok` = your Gemini key |
| `gemini_api_key` | Only for `byok` | — | Google AI Studio API key |
| `relay_url` | No | `wss://relay.herald.dev` | Custom relay endpoint (self-hosted) |

---

## Step 4 — Verify the plugin is connected

Restart Hermes (or start it fresh) and run:

```bash
hermes plugins list
```

You should see Herald in the list with a green status:

```
✓ herald-relay        connected   v0.x.x
  device: iPhone 15 Pro (••••a3f2)
  mode:   credits
  bursts used this month: 0 / 20 (free tier)
```

If you see `disconnected` or an error, check your device token and internet connection. Run `hermes herald diagnose` for a detailed connection test.

---

## Step 5 — Your first burst

Now let's see the full loop in action.

1. **Start a long-running task** — something that will take at least 30 seconds:

   ```bash
   hermes "Read the Hermes changelog on GitHub and write me a summary of everything added in the last 6 months"
   ```

2. **Put your laptop away.** Hermes is working. Your terminal can be backgrounded or in a different room.

3. **Your phone buzzes** — a Herald push notification appears:

   ```
   🔔 Hermes  •  Task complete
   "Here's the changelog summary — tap to review or ask a follow-up"
   ```

4. **Tap the notification.** Herald opens with a voice burst interface. Hold the mic button to record your reply (e.g. *"Great, now turn that into a tweet thread"*).

5. **Herald sends your voice burst** back to Hermes. The audio is transcribed, processed by your agent, and the response is pushed back to your phone — usually in under 15 seconds.

That's the full loop. One task, zero context-switching, a few cents of API cost.

---

## What's next?

- **[How It Works](/docs/how-it-works)** — architecture deep-dive, security model
- **[Plugin Configuration](/docs/plugin-config)** — all config options
- **[Billing](/docs/billing)** — BYOK vs Credits, pricing math
