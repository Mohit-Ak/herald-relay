"""Every install path a customer can follow must land in ~/.herald-relay, the
only place Herald Bridge updates itself, and name the config keys the code
actually reads. Found live: the READMEs and website said `pip install
hermes-herald` (not on PyPI; a squatter could claim it), `plugins.herald-relay`
(read by nothing) and `wss://relay.herald.app` (never existed)."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOCS = [REPO / "README.md", REPO / "plugin" / "README.md",
        *sorted((REPO / "website" / "docs").glob("*.md")),
        REPO / "website" / "src" / "pages" / "index.tsx"]

STALE = [
    (re.compile(r"(?m)^\s*['\"]?pip install (-U |--upgrade )?hermes-herald\b"), "pip install from PyPI"),
    (re.compile(r"plugins:\s*\n\s*herald-relay:"), "plugins.herald-relay config block"),
    (re.compile(r"'plugins:',\s*\n\s*'  herald-relay:'"), "plugins.herald-relay config block"),
    (re.compile(r"relay_url:\s*['\"]?wss://relay\.herald\.(app|dev)"), "dead relay URL"),
    (re.compile(r"hermes herald (diagnose|status|qr)"), "a command that doesn't exist"),
]


def test_no_install_path_teaches_an_unmanaged_or_stale_setup():
    bad = []
    for f in DOCS:
        text = f.read_text(encoding="utf-8")
        for rx, what in STALE:
            if rx.search(text):
                bad.append(f"{f.relative_to(REPO)}: {what}")
    assert not bad, bad


def test_install_docs_use_the_owned_venv():
    for f in (REPO / "README.md", REPO / "plugin" / "README.md",
              REPO / "website" / "docs" / "quickstart.md"):
        text = f.read_text(encoding="utf-8")
        assert "python3 -m venv ~/.herald-relay" in text, f
        assert "~/.herald-relay/bin/herald-tunnel setup" in text, f


def test_packaged_default_relay_is_the_live_one():
    from herald_relay import plugin, tunnel_cli
    src = Path(plugin.__file__).read_text()
    assert "relay.herald.app" not in src.replace("relay.herald.app never existed", "")
    assert tunnel_cli.DEFAULT_RELAY in src
