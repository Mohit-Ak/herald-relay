"""release_plugin.py: the signed manifest can name the Hermes release Herald
tested, the block survives later releases, and a bad block is never written."""
import argparse
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "plugin"))
import release_plugin as rp  # noqa: E402
from herald_relay import hermes_update  # noqa: E402

SHA = "c" * 40


def args(**kw):
    a = dict(hermes_tag="", hermes_rollout=None, hermes_off=False)
    a.update(kw)
    return argparse.Namespace(**a)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    monkeypatch.setattr(rp, "resolve_hermes_tag", lambda tag: SHA)


def test_new_hermes_release_starts_at_nobody():
    h = rp.hermes_block(None, args(hermes_tag="v0.23.3"))
    assert h["tag"] == "v0.23.3" and h["version"] == "0.23.3" and h["git_sha"] == SHA
    assert h["rollout_percent"] == 0
    assert hermes_update.pin_from({"hermes": h}) is not None


def test_block_is_carried_forward_and_widened():
    prev = rp.hermes_block(None, args(hermes_tag="v0.23.3", hermes_rollout=10))
    assert rp.hermes_block(prev, args()) == prev                      # a plugin release
    assert rp.hermes_block(prev, args(hermes_rollout=100))["rollout_percent"] == 100
    assert rp.hermes_block(prev, args(hermes_off=True)) is None


def test_a_new_tag_resets_the_rollout():
    prev = rp.hermes_block(None, args(hermes_tag="v0.23.3", hermes_rollout=100))
    assert rp.hermes_block(prev, args(hermes_tag="v0.24.0"))["rollout_percent"] == 0


def test_rejects_prerelease_tags(monkeypatch):
    monkeypatch.undo()  # the real resolver validates before any network call
    with pytest.raises(SystemExit):
        rp.resolve_hermes_tag("v0.23.3+canary.20261120T064609Z")
