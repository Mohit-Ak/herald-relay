---
id: quickstart
title: Quick Start
sidebar_position: 1
---

# Quick Start

The full, current guide is at **[heraldbridge.dev](https://heraldbridge.dev/docs/setup)**. This page is the short version for the machine that runs Hermes.

---

## Step 1: Get the Herald app

Install **Herald** from Google Play (Android) or the invitation you were sent (iPhone), and sign in with Google.

---

## Step 2: Turn on Hermes' API server

```bash
curl -fsSL https://heraldbridge.dev/install.sh -o install.sh
curl -fsSL https://heraldbridge.dev/install.sh.sha256 -o install.sh.sha256
sha256sum -c install.sh.sha256      # must print: install.sh: OK
bash install.sh
```

The installer enables Hermes' API server and creates `API_SERVER_KEY` in `~/.hermes/.env` if there isn't one.

---

## Step 3: Install Herald Bridge (it keeps itself up to date)

Install it into its own venv at `~/.herald-relay`. That is the only place it can update itself:

```bash
python3 -m venv ~/.herald-relay
~/.herald-relay/bin/pip install -U herald-bridge
~/.herald-relay/bin/herald-tunnel setup
```

`setup` registers this machine with the Herald relay, writes `herald.relay_url` and `herald.device_token` to `~/.hermes/config.yaml`, and installs a systemd user unit (or, without systemd, a Hermes scheduled check) that keeps the tunnel running after restarts. User units stop at logout unless lingering is on: `sudo loginctl enable-linger $USER`.

Then restart the Hermes gateway once so it serves the new key, and run the installer again (`bash install.sh`). It now prints **Relay paired** and a QR code that also works away from your Wi-Fi.

:::note Installed it somewhere else?
If Herald Bridge was installed with `pip install` into Hermes' own venv or with `pip --user`, `herald-tunnel setup` and the running tunnel move it into `~/.herald-relay` by themselves. Your device token and settings stay as they are, and from then on it updates itself. Developer checkouts (`pip install -e`) are never moved. The PyPI name is `herald-bridge` (`hermes-herald` on PyPI is a placeholder for the old name).
:::

---

## Step 4: Check it end to end

```bash
~/.herald-relay/bin/herald-tunnel status
```

`tunnel (this device)` answering OK means your phone can reach this Hermes through the relay. If it says it is not connected yet, wait a minute and check again.

---

## Step 5: Pair the phone

In the app: **Menu → Agents → Add another agent**, then scan the QR the installer printed, or paste its `herald://connect` link. The QR carries your agent's key, so don't share it.

---

## Updates

Herald Bridge checks for a new release every few hours. It installs the release next to the running copy, switches over only when nothing is in flight, and rolls back by itself if the new version doesn't come up. To pin a version, set `herald.auto_update: false` in `~/.hermes/config.yaml` (or `HERALD_BRIDGE_AUTO_UPDATE=0` in the tunnel's environment). See [Configuration](/docs/plugin-config).
