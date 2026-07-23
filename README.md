# Herald Relay

> Talk to your AI agent. From anywhere.

Herald is the mobile interface for [Hermes Agent](https://github.com/Mohit-Ak/hermes). It connects your local Hermes to your phone via push-notified burst conversations — no always-on call, no port forwarding.

## How it works

```
Your phone (idle)
  ↑ FCM push: "⚡ Hermes needs approval"
  ↓ tap → WebRTC burst (30s)
  ↑ voice reply → back to background
```

Hermes works in the background. Herald taps you on the shoulder when it needs you.

## Quick start

```bash
pip install hermes-herald
# Add to ~/.hermes/config.yaml:
# plugins:
#   herald-relay:
#     device_token: YOUR_TOKEN  # from the Herald app
```

## Billing

- **BYOK** — bring your own Gemini API key + $2/mo relay fee
- **Credits** — buy bursts at ~$0.05 each (20 free to start)

## Repo structure

```
backend/   FastAPI relay server (deploy to GCE or Cloud Run)
plugin/    hermes-herald Python plugin (pip install hermes-herald)
website/   Docusaurus marketing site
infra/     GCP deployment configs
```

## License

MIT
