---
id: plugin-config
title: Configuration
sidebar_position: 3
---

# Configuration

Herald Bridge reads the `herald:` block of `~/.hermes/config.yaml`. (Older pages said `plugins.herald-relay`; nothing reads that key.) `herald-tunnel setup` writes the block for you:

```yaml
# ~/.hermes/config.yaml
herald:
  relay_url: https://herald-api.eventhorizonlabs.xyz   # written by setup
  device_token: "…"                                     # written by setup; keep it secret
  # auto_update: false   # pin this version (default: updates itself)
```

| Key | Default | What it does |
|---|---|---|
| `relay_url` | `https://herald-api.eventhorizonlabs.xyz` | The Herald relay this machine dials out to. Change it only for a self-hosted relay (`herald-tunnel setup --relay-url …`). |
| `device_token` | registered by `setup` | Identifies this machine at the relay. `setup` keeps an existing one; `setup --new-token` replaces it (pair the phone again afterwards). |
| `auto_update` | `true` | `false` stops Herald Bridge from updating itself and refuses "update now" from the app. |

The key Herald uses to talk to Hermes is Hermes' own `API_SERVER_KEY` in `~/.hermes/.env`.

## Environment variables

Set these in the tunnel's environment. For the systemd unit, add an `Environment=` line to `~/.config/systemd/user/herald-tunnel.service`, then run `systemctl --user daemon-reload` and restart `herald-tunnel`.

| Variable | Default | What it does |
|---|---|---|
| `HERALD_BRIDGE_AUTO_UPDATE` | on | `0` turns automatic updates off (same as `herald.auto_update: false`). |
| `HERALD_BRIDGE_MIGRATE` | on | `0` stops a bridge installed outside `~/.herald-relay` from moving itself there. It then can't update itself. |
| `HERALD_BRIDGE_ROOT` | `~/.herald-relay` | The install root Herald Bridge manages and updates. |
| `HERALD_LOCAL_HERMES_URL` | `http://127.0.0.1:8642` | Where Hermes' API server listens. |
| `HERALD_RELAY_URL`, `HERALD_DEVICE_TOKEN` | none | Fallbacks when the `herald:` block is missing. |

## Commands

| Command | What it does |
|---|---|
| `herald-tunnel setup` | Register this machine, write the `herald:` block, install the service and Herald's skills. |
| `herald-tunnel status` | Check local Hermes, the relay, and this machine's tunnel end to end. |
| `herald-tunnel update` | Check for a Herald Bridge update now (it also does this by itself). |
| `herald-tunnel update --status` | Show the version, whether it updates itself, and the last result. |
| `herald-tunnel run` | Run the tunnel in the foreground (the service does this for you). |
