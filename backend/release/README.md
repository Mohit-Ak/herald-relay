# Herald Bridge release manifest

`plugin-manifest.json` here is what `GET /relay/plugin/manifest` serves (herald#505).
It is written ONLY by `scripts/release_plugin.py`, which signs it offline with the key in
Secret Manager `herald-relay-signing-key`. Never edit it by hand: a changed byte breaks the
signature and every machine refuses the update (fail closed).

No file here = no release offered (the endpoint answers 404 and machines stay as they are).
