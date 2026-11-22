# hermes-herald

Connect your local Hermes AI agent to Herald — voice-first conversations on mobile.

## Install in 2 commands

```bash
pip install hermes-herald
```

Then add to your Hermes config (`~/.hermes/config.yaml`):

```yaml
plugins:
  herald-relay:
    relay_url: wss://relay.herald.app
    device_token: YOUR_TOKEN  # Get this from the Herald app
    push_on_approval: true
    push_on_done: true
```

## How it works

The plugin dials OUT from your machine to Herald Cloud. No port forwarding needed.  
Hermes works normally; Herald notifies you on mobile when it needs you or finishes a task.

## Configuration

| Key | Default | Description |
|-----|---------|-------------|
| `relay_url` | `wss://relay.herald.app` | Herald Cloud WebSocket URL |
| `device_token` | `$HERALD_DEVICE_TOKEN` | Push token from the Herald mobile app |
| `push_on_approval` | `true` | Notify immediately when Hermes needs approval |
| `push_on_done` | `true` | Notify when a long-running task completes (>10 s) |

## Environment variables

- `HERALD_DEVICE_TOKEN` — fallback if `device_token` not in config
- `HERALD_LOCAL_HERMES_URL` — override local Hermes URL (default: `http://localhost:8642`)

## Architecture

```
Mobile App  ←→  Herald Cloud  ←(WebSocket)→  hermes-herald plugin  ←→  Local Hermes
```

1. Herald Cloud receives voice/text from your mobile app.
2. It forwards the HTTP request over the persistent WebSocket tunnel.
3. The plugin proxies it to your local Hermes API and streams the response back.
4. When Hermes fires an `approval_required` or completes a long task, the plugin
   sends a push trigger so Herald Cloud wakes your phone.

## Agent name and skills on stock Hermes

Herald reads the agent's name from `GET /api/soul` and its skills from
`GET /v1/skills`. Stock Hermes does not serve the first (404), and its
`/v1/skills` currently fails (500). The tunnel answers both paths itself, for
the default profile and `/p/<profile>/…`, whenever local Hermes can't: it asks
Hermes first and passes a working answer through untouched, otherwise it
checks the caller holds that profile's key and builds the same answer with
Hermes' own loaders (`agent_info_helper.py`, run with Hermes' interpreter),
falling back to reading `SOUL.md` and `skills/**/SKILL.md`. Nothing needs to
be patched into Hermes, so `hermes update` no longer breaks either.
