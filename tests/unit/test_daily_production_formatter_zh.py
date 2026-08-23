"""2026-08-22: reader-friendly underfilled-pool disclosure for the zh_cn
daily formatter, mirroring the English formatter's coverage -- no prior
test file exercised daily_production_formatter_zh.py's top-level render
function at all."""

from __future__ import annotations

from news_intel.delivery.daily_production_formatter_zh import render_production_daily_zh
from news_intel.pipeline.daily_selection import SelectionResult, Story


def _item(event_id, title, category, score):
    return {
        "event_id": event_id, "title": title, "category": category, "final_score": score,
        "excerpt": "一句完整的中文句子在这里。", "confirmation_state": "official", "entities": [],
        "sources": [{"publisher": "测试来源", "url": f"https://example.test/{event_id}",
                     "published_at": "2026-08-22T10:00:00+00:00"}],
    }


def test_underfilled_pool_marker_appears_below_threshold_zh():
    story = Story(primary=_item("E1", "一个真实的故事发生了", "市场与经济", 80.0))
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily_zh(result, "Aug 22", [])
    assert "质量与来源多样性标准" in rendered


def test_underfilled_pool_marker_absent_at_full_strength_zh():
    stories = [Story(primary=_item(f"E{i}", f"第 {i} 个故事发生了", "市场与经济", 80.0)) for i in range(6)]
    result = SelectionResult(top_stories=stories[:3], more_to_know=stories[3:])
    rendered = render_production_daily_zh(result, "Aug 22", [])
    assert "质量与来源多样性标准" not in rendered
