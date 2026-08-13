"""2026-08-13 reconciliation round: the materiality gate for recognition/
award/ranking stories, and BBC World/China RSS parsing (fixture-based, no
live network -- see the round-6 evidence directory for the live snapshot
validation)."""

from __future__ import annotations

from pathlib import Path

from news_intel.adapters.base import SourceHealthState
from news_intel.adapters.rss import RssAdapter
from news_intel.pipeline.event_integrity import is_unqualified_recognition_story
from news_intel.pipeline.publisher_family import publisher_family_of_url

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_glassdoor_best_ceo_story_is_excluded():
    """The exact 2026-08-12 reproduction: a "best CEO" ranking piece built
    from an employee-approval percentage, with no independently material
    consequence."""
    title = "NVIDIA CEO Tops Glassdoor's 2026 List of Best CEOs"
    excerpt = (
        "NVIDIA founder and CEO Jensen Huang is ranked No. 1 on Glassdoor's Best CEOs list for 2026. "
        "Huang topped the list, with 99% of employees approving of the job he does."
    )
    assert is_unqualified_recognition_story(title, excerpt) is True


def test_recognition_story_with_a_real_dollar_consequence_is_not_excluded():
    title = "CEO named to industry hall of fame after leading $2 billion acquisition"
    excerpt = "The honor follows the executive's role in closing a $2 billion acquisition this quarter."
    assert is_unqualified_recognition_story(title, excerpt) is False


def test_recognition_story_with_a_governance_consequence_is_not_excluded():
    title = "Board honors outgoing CEO as company announces resignation and succession plan"
    excerpt = "The board recognized the departing CEO's tenure as it announced her resignation, effective immediately."
    assert is_unqualified_recognition_story(title, excerpt) is False


def test_non_recognition_story_is_never_flagged():
    title = "Fed holds interest rates steady"
    excerpt = "The Federal Reserve held rates steady, surprising markets that expected a cut."
    assert is_unqualified_recognition_story(title, excerpt) is False


def test_employee_approval_percentage_alone_does_not_count_as_consequence():
    """The percent-magnitude signal is deliberately excluded from the
    "material consequence" check -- an approval-rating percentage must not
    self-certify a recognition piece as material."""
    title = "CEO wins award for leadership"
    excerpt = "The award cites a 95% approval rating among staff."
    assert is_unqualified_recognition_story(title, excerpt) is True


# --- BBC World / China RSS parsing (fixture-based) --------------------------


def test_bbc_world_rss_parses_with_canonical_urls_and_timestamps(monkeypatch):
    from tests.conftest import make_source_context

    ctx = make_source_context(id="bbc_world", url="https://feeds.bbci.co.uk/news/world/rss.xml")
    xml_text = (FIXTURES / "rss_sample_bbc_world.xml").read_text(encoding="utf-8")
    outcome = RssAdapter().parse(xml_text, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 2
    for item in outcome.items:
        assert item.canonical_url.startswith("https://www.bbc.co.uk/news/")
        assert item.published_at is not None
        assert item.language == "en"


def test_bbc_publisher_family_is_shared_across_world_and_china_feeds():
    """Publisher-family caps must apply jointly across bbc_world and
    bbc_china -- both normalize to the same registrable domain."""
    world_url = "https://www.bbc.co.uk/news/articles/c74grmy2v7ko"
    china_url = "https://www.bbc.co.uk/news/articles/c75g9777wwxo"
    assert publisher_family_of_url(world_url) == publisher_family_of_url(china_url)


def test_stale_xinhua_rss_was_not_added():
    """2026-08-13 evaluation rejected the proposed Xinhua RSS feed (newest
    items dated March 2017) -- confirmed absent from the source manifest."""
    from news_intel.settings import load_config

    config = load_config()
    urls = [s.url for s in config.sources.sources]
    assert not any("xinhuanet.com" in (u or "") for u in urls)
    assert not any("english.news.cn" in (u or "") for u in urls)
