"""`herald-tunnel status` must test the tunnel through the address the phone
uses: <relay>/hermes/d/<device_token>/health. The old check called
/hermes/health?device_token=..., which Herald's backend (the relay since the
Cloud Run move) answers 400, so a healthy tunnel always read FAIL — and an
agent following the setup message stopped there."""
import argparse
import io
import sys
import urllib.error
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from herald_relay import tunnel_cli  # noqa: E402

RELAY = "https://relay.example"
TOKEN = "devtok123"


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _run(monkeypatch, answer):
    seen = []

    def fake_urlopen(url, timeout=0):
        seen.append(url)
        code, body = answer(url)
        if code != 200:
            raise urllib.error.HTTPError(url, code, "x", {}, io.BytesIO(body.encode()))
        return _Resp(body.encode())

    monkeypatch.setattr(tunnel_cli.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(tunnel_cli, "_read_yaml_config",
                        lambda: {"relay_url": RELAY, "device_token": TOKEN})
    out = io.StringIO()
    with redirect_stdout(out):
        rc = tunnel_cli.cmd_status(argparse.Namespace())
    return rc, out.getvalue(), seen


def _herald_backend(tunnel_up: bool):
    """What the deployed relay answers, per path."""
    def answer(url):
        if url.endswith("/health") and "/hermes/d/" not in url and "/hermes/" not in url:
            return 200, '{"status":"ok"}'  # relay itself, or local Hermes
        if url.startswith(f"{RELAY}/hermes/d/{TOKEN}/health"):
            return (200, '{"status": "ok"}') if tunnel_up else \
                (503, '{"detail":{"error":"device_offline"}}')
        if "/hermes/health" in url:
            return 400, '{"detail":"Missing device_token header or query param"}'
        return 404, "{}"
    return answer


def test_a_connected_tunnel_reads_ok(monkeypatch):
    rc, out, seen = _run(monkeypatch, _herald_backend(tunnel_up=True))
    assert rc == 0, out
    assert f"{RELAY}/hermes/d/{TOKEN}/health" in seen
    assert "FAIL" not in out


def test_a_disconnected_tunnel_reads_fail_in_plain_words(monkeypatch):
    rc, out, _ = _run(monkeypatch, _herald_backend(tunnel_up=False))
    assert rc == 1
    assert "FAIL  tunnel (this device)" in out
    assert "not connected" in out
