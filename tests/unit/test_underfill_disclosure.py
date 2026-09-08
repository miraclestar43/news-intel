"""2026-08-29 production incident: the delivered zh_cn digests both ended
with "说明：今日仅有 N 条动态达到质量与来源多样性标准" -- telling the reader
that only 2-3 stories met the QUALITY bar. In both cases the real cause
was source concentration: every qualified candidate came from IT之家 or
量子位, and the per-publisher cap then bound. Blaming quality for a
publisher-cap outcome misrepresents the reader's own feed."""

from __future__ import annotations

from news_intel.delivery.daily_production_formatter import render_production_daily
from news_intel.delivery.daily_production_formatter_zh import render_production_daily_zh
from news_intel.pipeline.daily_selection import SelectionResult, Story


def _item(event_id, title, category, url):
    return {
        "event_id": event_id, "title": title, "category": category, "final_score": 80.0,
        "excerpt": "A complete sentence describing what happened.", "confirmation_state": "official",
        "entities": [], "sources": [{"publisher": "P", "url": url, "published_at": "2026-08-28T10:00:00+00:00"}],
    }


def _one_story():
    return [Story(primary=_item("E1", "A real story happened", "Markets & Economy", "https://a.test/1"))]


def test_thin_pool_is_reported_as_a_quality_outcome():
    result = SelectionResult(top_stories=_one_story(), more_to_know=[],
                              qualified_family_count=1, distinct_publisher_families=1)
    assert result.underfill_cause == "quality"
    assert "quality bar" in render_production_daily(result, "Aug 28", [])


def test_source_concentration_is_not_reported_as_a_quality_failure_en():
    """21 qualified stories from 2 publishers is a source-concentration
    outcome, not a quality one."""
    result = SelectionResult(top_stories=_one_story(), more_to_know=[],
                              qualified_family_count=21, distinct_publisher_families=2)
    assert result.underfill_cause == "source_concentration"
    rendered = render_production_daily(result, "Aug 28", [])
    assert "21 stories met today's quality bar" in rendered
    assert "concentrated, not diverse" in rendered
    assert "only 1 stories met" not in rendered
    # 2026-09-07: must not claim diversity it does not have
    assert "to preserve source diversity" not in rendered


def test_source_concentration_is_not_reported_as_a_quality_failure_zh():
    result = SelectionResult(top_stories=_one_story(), more_to_know=[],
                              qualified_family_count=21, distinct_publisher_families=2)
    rendered = render_production_daily_zh(result, "Aug 28", [])
    assert "共有 21 条动态达到质量标准" in rendered
    assert "全部来自 2 家媒体" in rendered
    assert "并不具备来源多样性" in rendered
    assert "仅有" not in rendered  # never implies the quality bar was the constraint
    # 2026-09-07: must not claim diversity it does not have
    assert "为保证来源多样性" not in rendered


def test_publisher_cap_is_distinguished_from_both():
    result = SelectionResult(top_stories=_one_story(), more_to_know=[],
                              qualified_family_count=12, distinct_publisher_families=5)
    assert result.underfill_cause == "publisher_cap"
    assert "per-publisher cap" in render_production_daily(result, "Aug 28", [])
    assert "单一来源数量上限" in render_production_daily_zh(result, "Aug 28", [])


def test_no_note_at_all_when_the_pool_is_full():
    stories = [Story(primary=_item(f"E{i}", f"Story {i}", "Markets & Economy", f"https://a.test/{i}"))
               for i in range(6)]
    result = SelectionResult(top_stories=stories[:3], more_to_know=stories[3:],
                              qualified_family_count=6, distinct_publisher_families=6)
    assert "Note:" not in render_production_daily(result, "Aug 28", [])
    assert "说明：" not in render_production_daily_zh(result, "Aug 28", [])
