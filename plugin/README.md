# hermes-herald (Herald Bridge)

Connect your local Hermes AI agent to Herald — voice-first conversations on mobile.

Full guide: <https://heraldbridge.dev/docs/setup>.

## Install (it keeps itself up to date)

Install into its own venv at `~/.herald-relay`, the only place Herald Bridge can
update itself. `hermes-herald` is not published on PyPI; install it from GitHub:

```bash
python3 -m venv ~/.herald-relay
~/.herald-relay/bin/pip install "git+https://github.com/Mohit-Ak/herald-relay#subdirectory=plugin"
~/.herald-relay/bin/herald-tunnel setup     # registers this machine, installs the service
~/.herald-relay/bin/herald-tunnel status    # local Hermes → relay → this tunnel, end to end
```

`setup` writes the `herald:` block of `~/.hermes/config.yaml` for you:

```yaml
herald:
  relay_url: https://herald-api.eventhorizonlabs.xyz
  device_token: <registered by setup>
  # auto_update: false   # pin this version
```

(Older docs said `plugins.herald-relay` and `wss://relay.herald.app`. Nothing reads
that key, and that host never existed.)

**Installed somewhere else?** A bridge installed with `pip install` into Hermes' own
venv, with `pip --user`, or into any other venv moves itself into `~/.herald-relay`:
`herald-tunnel setup` does it before writing anything, and a running tunnel does it
in the background once nothing is in flight (`herald_relay/migrate.py`). It installs
the release named by the signed manifest, rewrites only the `ExecStart` line of the
systemd user unit (and the keepalive script and skills), and re-execs from there. The
device token and settings live in `~/.hermes/config.yaml`, so pairing is unchanged.
Not moved: developer checkouts (`pip install -e`), `HERALD_BRIDGE_MIGRATE=0`, automatic
updates turned off, or a tunnel for a non-default `HERMES_HOME`.

## How it works

The plugin dials OUT from your machine to Herald Cloud. No port forwarding needed.  
Hermes works normally; Herald notifies you on mobile when it needs you or finishes a task.

## Configuration

| Key (`herald:` in `~/.hermes/config.yaml`) | Default | Description |
|-----|---------|-------------|
| `relay_url` | `https://herald-api.eventhorizonlabs.xyz` | Herald relay this machine dials out to |
| `device_token` | registered by `setup` | Identifies this machine at the relay |
| `auto_update` | `true` | `false` pins this version |

## Environment variables

- `HERALD_RELAY_URL`, `HERALD_DEVICE_TOKEN` — fallbacks if the `herald:` block is missing
- `HERALD_LOCAL_HERMES_URL` — override local Hermes URL (default: `http://127.0.0.1:8642`)
- `HERALD_BRIDGE_AUTO_UPDATE=0` — turn automatic updates off (or `herald.auto_update: false`
  in `~/.hermes/config.yaml`). Also refuses "update now" from the app.
- `HERALD_BRIDGE_MIGRATE=0` — don't move a bridge installed elsewhere into `~/.herald-relay`.

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

Only a venv Herald owns is updated: `~/.herald-relay`, `~/.herald/venv` (Herald Cloud), or
`HERALD_BRIDGE_ROOT`. A bridge installed anywhere else moves itself into `~/.herald-relay`
first (see "Installed somewhere else?" above). `herald-tunnel update` asks for an update now;
`herald-tunnel update --status` shows the last result. Machines on 0.7.x and older have no
updater: they need one manual `pip install -U` to reach 0.8.0 (0.8.1+ when installed outside
`~/.herald-relay`, so the move kicks in).

Releasing: `python scripts/release_plugin.py --rollout 10` (builds, attaches the wheel to
GitHub release `plugin-v<version>`, signs with Secret Manager `herald-relay-signing-key`,
writes `backend/release/plugin-manifest.json`), merge that file, then
`--resign --rollout 100` when the first slice is healthy. The relay counts machines per
version at `GET /relay/plugin/versions` (header `X-Herald-Ops-Token`).

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
