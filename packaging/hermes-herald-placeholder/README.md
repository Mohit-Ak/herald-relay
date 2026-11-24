# hermes-herald (placeholder)

This is not Herald Bridge. The name is held so nobody else can publish code
under it. The package contains no code, makes no network calls and depends on
nothing: importing `hermes_herald` raises `ImportError` and the
`hermes-herald` command prints the install instructions and exits 1.

Herald Bridge is published as **herald-bridge**. Install it into its own venv,
the only place it updates itself:

```
python3 -m venv ~/.herald-relay && ~/.herald-relay/bin/pip install herald-bridge && ~/.herald-relay/bin/herald-tunnel setup
```

Source: https://github.com/Mohit-Ak/herald-relay
