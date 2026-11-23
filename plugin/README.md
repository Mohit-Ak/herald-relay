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
- `HERALD_BRIDGE_AUTO_UPDATE=0` — turn automatic updates off (or `herald.auto_update: false`
  in `~/.hermes/config.yaml`). Also refuses "update now" from the app.

## Automatic updates (0.8.0+)

`herald-tunnel run` keeps Herald Bridge up to date by itself (`herald_relay/bridge_update.py`):

1. On start, every 6 h (± 30 min jitter), when the relay hints a newer release on connect,
   and when the app or an operator asks ("update now"), it fetches
   `GET <relay>/relay/plugin/manifest`: a DSSE envelope signed offline with Ed25519
   (`scripts/release_plugin.py`; the public key is compiled into the plugin). Expired
   manifests and versions not newer than the running one are refused.
2. Machines in the release's `rollout_percent` slice (stable per machine) download the wheel
   (size-capped, sha256-checked) and install it into a NEW venv,
   `~/.herald-relay-versions/<version>`, which must pass `herald-tunnel selfcheck`.
3. When nothing has been in flight for 20 s (no run, no forwarded request, no approval), the
   stable path `~/.herald-relay` is flipped to the new venv with one `rename(2)` and the
   tunnel re-execs itself. systemd, the keepalive and skills keep using `~/.herald-relay`.
4. The new version is on probation until it registers with the relay. If it crash-loops
   (3 starts) or cannot register while the relay is reachable, `~/.herald-relay` is flipped
   back, the version is marked bad and the previous build restarts.

Only a venv Herald owns is touched: `~/.herald-relay`, `~/.herald/venv` (Herald Cloud), or
`HERALD_BRIDGE_ROOT`. `herald-tunnel update` asks for an update now;
`herald-tunnel update --status` shows the last result. Machines on 0.7.x and older have no
updater: they need one manual `pip install -U` to reach 0.8.0.

Releasing: `python scripts/release_plugin.py --rollout 10` (builds, attaches the wheel to
GitHub release `plugin-v<version>`, signs with Secret Manager `herald-relay-signing-key`,
writes `backend/release/plugin-manifest.json`), merge that file, then
`--resign --rollout 100` when the first slice is healthy. The relay counts machines per
version at `GET /relay/plugin/versions` (header `X-Herald-Ops-Token`).

## Files the agent makes (0.9.0+)

When the agent writes `MEDIA:/absolute/path` in a reply, the Herald app shows the file: a
picture inline, anything else as an attachment to open, save or share. The app fetches it
from this tunnel (`herald_relay/local_files.py`), never from a public link:

| Request | Answer |
|---|---|
| `GET [/p/<profile>]/_herald/file?path=<abs>&info=1` | `{name, size, mime, kind, mtime, max_bytes, chunk_bytes}` |
| `GET …/_herald/file?path=<abs>&offset=<n>&length=<n>&mtime=<m>` | one piece (≤ 2 MiB) as `{…, offset, b64, eof}` |
| `GET …/_herald/file?path=<abs>&w=<px>` | a picture shrunk to `w` (≤ 1600), WebP, no EXIF/GPS: `{mime, b64, width, height}` |

- Only the holder of that profile's API key (checked against local Hermes).
- Only the home folder minus hidden folders, Hermes' own media folders (generated images,
  screenshots) and the user's own temp files. Credentials by name (`id_rsa`, `*.pem`,
  `credentials.json`, `.env*`, …) and anything that starts like a private key are refused
  everywhere. `HERALD_FILE_ROOTS` (os.pathsep list) replaces the home folder.
- Files over 50 MB (`HERALD_FILE_MAX_MB`) are not sent; the app says how big the file is.
- Logs carry status and byte counts, never a path.

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
