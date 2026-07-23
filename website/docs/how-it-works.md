---
id: how-it-works
title: How It Works
sidebar_position: 2
---

# How It Works

This page explains the Herald relay architecture, why no port forwarding is needed, the security model, and the economics of the burst model.

---

## Architecture overview

Herald is a three-component system:

```
┌─────────────────────┐          ┌──────────────────────┐          ┌──────────────────────┐
│                     │          │                       │          │                      │
│   Hermes (laptop)   │◄────────►│   Herald Relay        │◄────────►│   Herald (phone)     │
│   + herald plugin   │  WSS/TLS │   relay.herald.dev    │  FCM/APNs│   iOS / Android      │
│                     │          │                       │          │                      │
└─────────────────────┘          └──────────────────────┘          └──────────────────────┘
        │                                  │                                  │
   Opens outbound                  Stateless message               Receives push
   WebSocket to relay              broker. Never stores            notification.
   on startup.                     message content.                Opens burst on tap.
```

**Data flow for a completed task:**

1. Hermes finishes a task and calls `herald.notify(message)`.
2. The herald plugin — already connected via an outbound WebSocket — sends the notification payload to the relay.
3. The relay looks up the registered device token and forwards a push via APNs (iOS) or FCM (Android).
4. The phone wakes, displays the notification. If the user taps, Herald opens a voice burst session.
5. The burst audio is streamed back through the relay to Hermes for processing.
6. Hermes's response is pushed back to the phone. Channel closes.

---

## Why no port forwarding is needed

Most "remote AI" setups require the AI machine to have a public IP or VPN — because your phone needs to reach it. Herald flips this.

The Hermes herald plugin **dials outbound** to `relay.herald.dev` over a standard TLS WebSocket (port 443). This works from:

- A laptop behind home NAT
- A corporate network with an outbound-only firewall
- A cloud VM with no inbound security group rules
- A Raspberry Pi on a mobile hotspot

The relay holds this persistent connection. When your phone taps a notification, the relay routes the burst through the *already-open* connection from Hermes — no inbound connection is ever needed.

---

## Security model

| Concern | How Herald handles it |
|---|---|
| **Device token exposure** | Tokens are hashed (SHA-256) before storage. A leaked token cannot be reverse-engineered to identify your device. |
| **Message confidentiality** | All relay traffic is TLS 1.3. The relay is a stateless forwarder — it never persists message content to disk. |
| **Authentication** | Each outbound connection from Hermes is authenticated with a signed JWT derived from your device token. Replay attacks are blocked with a 30-second nonce window. |
| **Burst audio** | Audio is end-to-end encrypted between the Herald app and the Hermes plugin using a session key negotiated at burst-open time. The relay cannot decrypt audio payloads. |
| **Self-hosting** | You can run your own relay (see `relay_url` config option). The relay is open-source under MIT. |

---

## The burst model

### Why not always-on?

An always-on voice assistant holds an open audio stream, continuously transcribing background noise and silence. At typical STT rates, that costs **$0.50–$2.00/hour** — even when you're not talking.

Herald uses a **push-notified burst model** instead:

- The audio channel is **closed by default**.
- It only opens when you **explicitly tap** a notification.
- A typical burst lasts 8–20 seconds.
- The channel closes as soon as the agent responds.

### Cost comparison

| | Always-on (1 hr active) | Herald burst (8s) |
|---|---|---|
| STT (speech-to-text) | ~$0.36 | ~$0.005 |
| LLM inference | continuous | one call |
| TTS (text-to-speech) | continuous | one response |
| **Total** | **$0.50–$2.00** | **~$0.05** |

For a user who does 10 "check-ins" per day, always-on costs ~$15–60/month in API alone. Herald bursts cost ~$0.50/month.

### Burst vs always-on comparison

| Feature | Always-on | Herald Burst |
|---|---|---|
| Latency to first word | Immediate | ~2s (tap → open) |
| API cost per session-hour | $0.50–$2.00 | $0.03–$0.10 |
| Works from lock screen | ✗ | ✓ |
| Works when laptop lid is closed | ✗ | ✓ |
| Works across network boundaries | Requires VPN | ✓ |
| Ambient listening | ✓ | ✗ (by design) |
| Battery impact on phone | High | Negligible |
| Privacy (no idle audio) | ✗ | ✓ |

The trade-off is clear: bursts give up sub-second "hey siri" latency in exchange for vastly lower cost, better privacy, and genuine anywhere access.

---

## Relay infrastructure

The Herald relay is hosted on Fly.io with edge nodes in North America, Europe, and Asia-Pacific. Connections are automatically routed to the nearest edge. Uptime SLA: 99.9%.

The relay source code is available at [github.com/nousresearch/herald-relay](https://github.com/nousresearch/herald-relay) under the MIT license. You can self-host it on any machine with Docker in under 10 minutes.
