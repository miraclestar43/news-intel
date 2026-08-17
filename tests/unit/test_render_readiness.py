"""2026-08-17 content-quality repair: why_it_matters()/chinese_takeaway()
must be story-specific (name the actual entity/action/amount), never a
bare per-category template."""

from __future__ import annotations

from news_intel.pipeline.render_readiness import chinese_takeaway, is_render_ready, why_it_matters


def _primary(**overrides) -> dict:
    base = {
        "title": "NVIDIA completes $8B acquisition of RunAI",
        "excerpt": "NVIDIA completes its previously announced $8 billion acquisition of RunAI, "
        "an Israeli AI orchestration startup, expanding its software stack.",
    }
    base.update(overrides)
    return base


def test_why_it_matters_names_entity_and_event_type_not_just_category():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert "NVIDIA" in result
    assert "acquisition" in result.lower()
    generic = (
        "Why it matters: ranks among today's most material developments in "
        "AI/semiconductor infrastructure and competitiveness."
    )
    assert result != generic


def test_why_it_matters_includes_dollar_amount_when_present():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert "$8" in result or "8" in result


def test_chinese_takeaway_names_entity_not_generic_category_template():
    """Reject the exact generic template this repair targets."""
    result = chinese_takeaway("AI & Semiconductors", entities=["NVIDIA"], primary=_primary())
    assert result != "看点：AI/半导体产业格局的重要动态。"
    assert "NVIDIA" in result


def test_chinese_takeaway_without_entities_falls_back_to_category_floor_only():
    """Documented, honest floor: with genuinely no extractable entity/
    event-type signal, the category-only line is still produced (never a
    reader-facing placeholder), but this is the exception, not the norm."""
    result = chinese_takeaway("AI & Semiconductors")
    assert result == "看点：AI/半导体产业格局的重要动态。"


def test_why_it_matters_with_geopolitical_event_names_the_specific_action():
    primary = {
        "title": "Israeli airstrikes hit targets in southern Lebanon amid Hezbollah tension",
        "excerpt": "Israeli airstrikes hit multiple targets in southern Lebanon on Sunday, killing "
        "several people, as tension with Hezbollah escalates despite an earlier ceasefire.",
    }
    result = why_it_matters(primary, None, "Politics", entities=["Lebanon"])
    assert "Lebanon" in result
    assert "ceasefire" in result.lower() or "conflict" in result.lower()


def test_chinese_takeaway_with_geopolitical_event_is_story_specific():
    primary = {
        "title": "Israeli airstrikes hit targets in southern Lebanon amid Hezbollah tension",
        "excerpt": "Israeli airstrikes hit multiple targets in southern Lebanon on Sunday, killing "
        "several people, as tension with Hezbollah escalates despite an earlier ceasefire.",
    }
    result = chinese_takeaway("Politics", entities=["Lebanon"], primary=primary)
    assert result != "看点：国内政策的重要动态。"
    assert "Lebanon" in result


def test_no_dangling_or_truncated_output():
    result = why_it_matters(_primary(), None, "AI & Semiconductors", entities=["NVIDIA"])
    assert not result.rstrip().endswith("...")
    assert not result.rstrip().endswith("…")
    assert result.rstrip().endswith(".")

    zh = chinese_takeaway("AI & Semiconductors", entities=["NVIDIA"], primary=_primary())
    assert not zh.rstrip().endswith("...")
    assert not zh.rstrip().endswith("…")
    assert zh.rstrip().endswith("。")


def test_is_render_ready_unchanged_by_this_repair():
    assert is_render_ready(_primary(), None) is True
    assert is_render_ready({"title": "", "excerpt": None}, None) is False
