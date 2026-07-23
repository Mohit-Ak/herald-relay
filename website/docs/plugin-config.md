---
id: plugin-config
title: Plugin Configuration
sidebar_position: 3
---

# Plugin Configuration

All Herald configuration lives under the `plugins.herald-relay` key in `~/.hermes/config.yaml`. This page documents every available option.

---

## Full example

```yaml
# ~/.hermes/config.yaml

plugins:
  herald-relay:
    enabled: true
    device_token: "a1b2c3d4e5f6..."      # Required — from the Herald mobile app
    mode: credits                          # 'credits' | 'byok'

    # ── BYOK mode only ──────────────────────────────────────────────
    # gemini_api_key: "AIzaSyXXXXXXX"    # Google AI Studio key
    # gemini_model: "gemini-1.5-flash"   # Optional — default shown

    # ── Relay connection ────────────────────────────────────────────
    # relay_url: "wss://relay.herald.dev" # Default — override for self-hosted
    # reconnect_interval: 5               # Seconds between reconnect attempts
    # max_reconnect_attempts: 0           # 0 = retry forever

    # ── Notifications ───────────────────────────────────────────────
    # notify_on_complete: true            # Notify when a Hermes task finishes
    # notify_on_error: true               # Notify when Hermes encounters an error
    # notify_on_question: true            # Notify when Hermes needs user input
    # notification_title: "Hermes"        # Override the notification title

    # ── Burst behaviour ─────────────────────────────────────────────
    # burst_timeout: 30                   # Seconds before an unanswered burst expires
    # max_burst_duration: 60             # Maximum seconds of audio per burst
    # tts_voice: "en-US-Standard-J"      # Google TTS voice for responses

    # ── Debug ───────────────────────────────────────────────────────
    # log_level: "info"                   # 'debug' | 'info' | 'warning' | 'error'
```

---

## Option reference

### Core options

| Option | Type | Default | Required | Description |
|---|---|---|---|---|
| `enabled` | boolean | `true` | No | Set to `false` to disable Herald without removing the config. |
| `device_token` | string | — | **Yes** | Device token from the Herald mobile app. Identifies your phone as the push target. |
| `mode` | string | `"credits"` | No | `credits` — Herald manages API keys and bills you per burst. `byok` — you supply your own Gemini API key; only the relay fee applies. |

### BYOK options *(only when `mode: byok`)*

| Option | Type | Default | Description |
|---|---|---|---|
| `gemini_api_key` | string | — | Your Google AI Studio API key. Get one free at [aistudio.google.com](https://aistudio.google.com/app/apikey). |
| `gemini_model` | string | `"gemini-1.5-flash"` | Which Gemini model to use for STT post-processing and TTS. `gemini-1.5-flash` is recommended for cost; `gemini-1.5-pro` for quality. |

### Relay connection options

| Option | Type | Default | Description |
|---|---|---|---|
| `relay_url` | string | `"wss://relay.herald.dev"` | WebSocket relay endpoint. Override to use a self-hosted relay. |
| `reconnect_interval` | integer | `5` | Seconds between reconnect attempts after a dropped connection. |
| `max_reconnect_attempts` | integer | `0` | Maximum reconnect attempts. `0` means retry forever. |

### Notification options

| Option | Type | Default | Description |
|---|---|---|---|
| `notify_on_complete` | boolean | `true` | Send a push notification when a Hermes task finishes successfully. |
| `notify_on_error` | boolean | `true` | Send a push notification if Hermes encounters an unhandled error. |
| `notify_on_question` | boolean | `true` | Send a push notification when Hermes pauses and needs your input to continue. |
| `notification_title` | string | `"Hermes"` | The title displayed on push notifications. Useful if you run multiple Hermes instances. |

### Burst behaviour options

| Option | Type | Default | Description |
|---|---|---|---|
| `burst_timeout` | integer | `30` | Seconds Hermes waits for a burst reply after sending a notification. After timeout, Hermes continues with a best-effort response or pauses. |
| `max_burst_duration` | integer | `60` | Maximum audio duration (seconds) for a single burst. Longer recordings are truncated. |
| `tts_voice` | string | `"en-US-Standard-J"` | Google Cloud TTS voice ID for reading Hermes responses back to you. See the [TTS voice list](https://cloud.google.com/text-to-speech/docs/voices). |

### Debug options

| Option | Type | Default | Description |
|---|---|---|---|
| `log_level` | string | `"info"` | Log verbosity for the herald-relay plugin. Set to `"debug"` to see raw WebSocket frames. |

---

## Minimal config (Credits mode)

```yaml
plugins:
  herald-relay:
    device_token: "your-token-here"
```

Everything else uses defaults. This is the recommended starting config.

---

## Minimal config (BYOK mode)

```yaml
plugins:
  herald-relay:
    device_token: "your-token-here"
    mode: byok
    gemini_api_key: "AIzaSyYOUR_KEY_HERE"
```

---

## Diagnosing connection issues

Run the built-in diagnostic tool after saving your config:

```bash
hermes herald diagnose
```

This tests: config parsing → relay reachability → authentication → device token validity → push delivery.
