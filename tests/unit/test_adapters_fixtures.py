"""Deterministic, network-free fixture tests for every adapter type.

config/sources.yaml currently maps these adapter types onto >=12 configured
sources (10 rss, 1 json_federal_register, 1 json_hn, 2 ical, 1 gdelt,
2 html_index, 3 social_stub) — see test_source_manifest.py for the count
assertion. English and Chinese content are both covered (rss/federal
register/hn/ical/gdelt fixtures are English; the gov.cn/PBOC html_index
fixtures are Chinese).
"""

from __future__ import annotations

from pathlib import Path

from news_intel.adapters.base import SourceHealthState
from news_intel.adapters.federal_register import FederalRegisterAdapter
from news_intel.adapters.gdelt import GdeltAdapter
from news_intel.adapters.hn import story_to_item
from news_intel.adapters.html_index import HtmlIndexAdapter
from news_intel.adapters.ical import IcalAdapter
from news_intel.adapters.rss import RssAdapter
from news_intel.adapters.social_stub import SocialStubAdapter

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def test_rss_adapter_parses_items(monkeypatch):
    from tests.conftest import make_source_context

    ctx = make_source_context(id="nvidia_newsroom", url="https://nvidianews.nvidia.com/releases.xml")
    xml_text = (FIXTURES / "rss_sample_nvidia.xml").read_text(encoding="utf-8")
    outcome = RssAdapter().parse(xml_text, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 2
    titles = {i.title for i in outcome.items}
    assert "NVIDIA Announces New AI Chip Export Rules Compliance Update" in titles
    assert all(i.language == "en" for i in outcome.items)
    assert outcome.new_cursor is not None


def test_rss_adapter_degrades_on_garbage(monkeypatch):
    from tests.conftest import make_source_context

    ctx = make_source_context()
    outcome = RssAdapter().parse("not xml at all {{{", ctx, cursor=None)
    assert outcome.health_state == SourceHealthState.DEGRADED


def test_federal_register_adapter_parses_and_stops_at_cursor():
    from tests.conftest import make_source_context

    ctx = make_source_context(id="federal_register", adapter="json_federal_register")
    json_text = (FIXTURES / "federal_register_sample.json").read_text(encoding="utf-8")

    outcome = FederalRegisterAdapter().parse(json_text, ctx, cursor=None)
    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 2
    assert outcome.new_cursor == "2026-16501"

    # Re-running with the new cursor should yield nothing new (idempotent).
    outcome2 = FederalRegisterAdapter().parse(json_text, ctx, cursor="2026-16501")
    assert outcome2.items == []


def test_hn_story_to_item_filters_non_stories():
    from tests.conftest import make_source_context

    ctx = make_source_context(id="hn_top", adapter="json_hn")
    story = {"type": "story", "url": "https://example.test/a", "title": "Some story", "time": 1785000000, "score": 250}
    item = story_to_item(story, 123, ctx)
    assert item is not None
    assert item.metadata["hn_score"] == 250

    job_posting = {"type": "job", "title": "We're hiring"}
    assert story_to_item(job_posting, 124, ctx) is None

    comment_only = {"type": "story", "title": "No url story"}
    assert story_to_item(comment_only, 125, ctx) is None


def test_ical_adapter_parses_calendar_events():
    from tests.conftest import make_source_context

    ctx = make_source_context(id="bls_calendar", adapter="ical")
    ics_text = (FIXTURES / "ics_sample.ics").read_text(encoding="utf-8")
    outcome = IcalAdapter().parse(ics_text, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 2
    assert any("Consumer Price Index" in i.title for i in outcome.items)
    assert all(i.published_at is not None for i in outcome.items)


def test_gdelt_adapter_filters_missing_title():
    from tests.conftest import make_source_context

    ctx = make_source_context(id="gdelt_doc", adapter="gdelt")
    json_text = (FIXTURES / "gdelt_sample.json").read_text(encoding="utf-8")
    outcome = GdeltAdapter().parse(json_text, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 1
    assert "Federal Reserve" in outcome.items[0].title


def test_html_index_adapter_parses_chinese_gov_source():
    """Selector verified against the real live page on 2026-08-01 (see
    config/sources.yaml comment); fixture mirrors that markup."""
    from tests.conftest import make_source_context

    ctx = make_source_context(
        id="gov_cn_policy",
        adapter="html_index",
        url="https://www.gov.cn/zhengce/index.htm",
        selectors={"item": "div.item03 ul li", "title": "a", "link": "a"},
    )
    html = (FIXTURES / "html_index_gov_cn.html").read_text(encoding="utf-8")
    outcome = HtmlIndexAdapter().parse(html, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 2
    assert outcome.items[0].language == "zh"
    assert outcome.items[0].canonical_url.startswith("https://www.gov.cn/")


def test_html_index_adapter_parses_pboc_source():
    """Adapter-parsing correctness only. The pboc_policy source entry itself
    is `enabled: false` in config/sources.yaml because pbc.gov.cn/robots.txt
    disallows all crawlers except Baiduspider -- this test does not imply the
    source is active."""
    from tests.conftest import make_source_context

    ctx = make_source_context(
        id="pboc_policy",
        adapter="html_index",
        url="http://www.pbc.gov.cn/tiaofasi/144941/144979/index.html",
        selectors={"item": "ul.list li", "title": "a", "link": "a"},
    )
    html = (FIXTURES / "html_index_pboc.html").read_text(encoding="utf-8")
    outcome = HtmlIndexAdapter().parse(html, ctx, cursor=None)

    assert outcome.health_state == SourceHealthState.HEALTHY
    assert len(outcome.items) == 1
    assert "人民银行" in outcome.items[0].title


def test_html_index_adapter_degrades_on_selector_mismatch():
    from tests.conftest import make_source_context

    ctx = make_source_context(
        id="gov_cn_policy",
        adapter="html_index",
        selectors={"item": "div.does-not-exist", "title": "a", "link": "a"},
    )
    html = (FIXTURES / "html_index_gov_cn.html").read_text(encoding="utf-8")
    outcome = HtmlIndexAdapter().parse(html, ctx, cursor=None)
    assert outcome.health_state == SourceHealthState.DEGRADED


def test_social_stub_always_unavailable_never_scrapes():
    from tests.conftest import make_source_context

    ctx = make_source_context(
        id="social_trumpdailyposts",
        adapter="social_stub",
        note="Mirror account, not an official channel.",
    )
    outcome = SocialStubAdapter().fetch(ctx, cursor=None)
    assert outcome.health_state == SourceHealthState.UNAVAILABLE
    assert outcome.items == []
    assert "Mirror" in outcome.health_detail
