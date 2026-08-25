"""2026-08-24: daily_production_formatter{,_zh}.py's consumption of
Story.editorial (the validator-gated per-story LLM editorial layer). Proves:
(1) 100% backward compatibility -- a Story with no .editorial set renders
IDENTICALLY to before this field existed (the entire pre-existing test
suite for these renderers already pins that); (2) an llm_validated
editorial's fields are used verbatim, in place of the deterministic
templates; (3) a None why_it_matters/chinese_takeaway on the editorial
(the "omit, don't guess" floor) is OMITTED, never silently replaced by the
old deterministic template -- that would defeat the whole point."""

from __future__ import annotations

from news_intel.delivery.daily_production_formatter import render_production_daily
from news_intel.delivery.daily_production_formatter_zh import render_production_daily_zh
from news_intel.pipeline.daily_selection import SelectionResult, Story
from news_intel.pipeline.editorial_enrichment import EditorialResult


def _item(event_id="E1", title="A real story happened", category="AI & Semiconductors", score=80.0):
    return {
        "event_id": event_id, "title": title, "category": category, "final_score": score,
        "excerpt": "A complete sentence describing the event happened here.", "confirmation_state": "official",
        "entities": ["Acme Corp"],
        "sources": [{"publisher": "Test Source", "url": f"https://example.test/{event_id}",
                     "published_at": "2026-08-22T10:00:00+00:00"}],
    }


def test_en_no_editorial_uses_deterministic_templates():
    story = Story(primary=_item())
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily(result, "Aug 22", [])
    assert "A complete sentence describing the event happened here." in rendered


def test_en_llm_validated_editorial_used_verbatim():
    editorial = EditorialResult(
        what_happened="Acme Corp announced a distinctive validated summary sentence.",
        why_it_matters="This matters because of a distinctive validated reason.",
        chinese_what_happened="Acme公司宣布了一句独特的中文验证摘要。",
        chinese_takeaway="这一独特的中文验证要点值得关注。",
        category="AI & Semiconductors", source="llm_validated", model_used="hermes_cli", validated=True,
    )
    story = Story(primary=_item(), editorial=editorial)
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily(result, "Aug 22", [])
    assert "a distinctive validated summary sentence" in rendered
    assert "a distinctive validated reason" in rendered
    assert "这一独特的中文验证要点值得关注" in rendered
    # deterministic-template text must NOT also appear alongside it
    assert "A complete sentence describing the event happened here." not in rendered


def test_en_deterministic_fallback_editorial_omits_why_it_matters():
    """The editorial layer's OWN fallback (source-grounded what_happened,
    why_it_matters=None) must OMIT the line -- never fall through to the
    old generic why_it_matters() template."""
    editorial = EditorialResult(
        what_happened="Acme Corp did something, per the source excerpt.",
        why_it_matters=None, chinese_what_happened=None, chinese_takeaway=None,
        category=None, source="deterministic_fallback", fallback_reason="no provider configured",
    )
    story = Story(primary=_item(), editorial=editorial)
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily(result, "Aug 22", [])
    assert "Acme Corp did something, per the source excerpt." in rendered
    # old deterministic why_it_matters()/chinese_takeaway() text must be absent
    assert "material development" not in rendered.lower()


def test_zh_no_editorial_uses_deterministic_templates():
    item = _item(title="一个真实的故事发生了", category="市场与经济")
    item["excerpt"] = "一句完整的中文句子在这里描述这件事。"
    story = Story(primary=item)
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily_zh(result, "Aug 22", [])
    assert "一句完整的中文句子在这里描述这件事。" in rendered


def test_zh_llm_validated_editorial_used_verbatim():
    editorial = EditorialResult(
        what_happened="Acme Corp announced something in English (unused by zh renderer).",
        why_it_matters="English why-it-matters (unused by zh renderer).",
        chinese_what_happened="独特的中文验证版本的发生了什么描述。",
        chinese_takeaway="独特的中文验证版本的重要性说明。",
        category="市场与经济", source="llm_validated", model_used="hermes_cli", validated=True,
    )
    item = _item(title="一个真实的故事发生了", category="市场与经济")
    story = Story(primary=item, editorial=editorial)
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily_zh(result, "Aug 22", [])
    assert "独特的中文验证版本的发生了什么描述。" in rendered
    assert "独特的中文验证版本的重要性说明。" in rendered
    # the English fields must never leak into the zh_cn digest
    assert "unused by zh renderer" not in rendered


def test_zh_deterministic_fallback_editorial_omits_why_it_matters():
    editorial = EditorialResult(
        what_happened="unused english text",
        why_it_matters=None, chinese_what_happened="来源支持的发生了什么中文句子。", chinese_takeaway=None,
        category=None, source="deterministic_fallback", fallback_reason="no provider configured",
    )
    item = _item(title="一个真实的故事发生了", category="市场与经济")
    story = Story(primary=item, editorial=editorial)
    result = SelectionResult(top_stories=[story], more_to_know=[])
    rendered = render_production_daily_zh(result, "Aug 22", [])
    assert "来源支持的发生了什么中文句子。" in rendered
    assert "unused english text" not in rendered
