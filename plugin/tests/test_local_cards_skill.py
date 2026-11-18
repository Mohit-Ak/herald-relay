"""Local agents get the herald-cards skill: how and when to show cards in the
Herald app, owned by the agent (not injected per run by Herald). The text is
the same as the cloud copy in herald's backend/cloud/skills/herald-cards."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import herald_relay  # noqa: E402
from herald_relay import local_skills  # noqa: E402


def test_cards_skill_is_installed(tmp_path):
    local_skills.install(tmp_path)
    text = (tmp_path / "skills/herald/herald-cards/SKILL.md").read_text()
    assert text.startswith("---\nname: herald-cards\n")
    assert text == local_skills.CARDS_SKILL


def test_cards_skill_says_when_where_and_how():
    s = local_skills.CARDS_SKILL
    # proactive, but not for trivial replies
    assert "don't wait to be asked" in s and "what time is it" in s
    # Herald only: never raw fences on Telegram or the CLI
    assert "Only on Herald" in s and "Telegram" in s
    # never read aloud: the sentence must stand alone
    assert "never read aloud" in s
    # the A2UI v1.0 format and the copy card
    assert "```a2ui" in s and "createSurface" in s and "updateComponents" in s
    assert "https://a2ui.org/specification/v1_0/catalogs/basic/catalog.json" in s
    assert "```herald-card" in s


def test_plugin_version_announces_the_skill():
    # Herald's backend sends the long card guide to agents without the skill;
    # 0.6.0 is the first plugin that installs it.
    major, minor, *_ = (int(x) for x in herald_relay.__version__.split("."))
    assert (major, minor) >= (0, 6)


def test_matches_the_cloud_copy():
    """Only where a herald checkout sits next to this one (not in CI)."""
    cloud = Path.home() / "projects/herald/backend/cloud/skills/herald-cards/SKILL.md"
    if not cloud.exists():
        pytest.skip("herald checkout (with the skill) not here")
    assert cloud.read_text() == local_skills.CARDS_SKILL
