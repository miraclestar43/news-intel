"""2026-08-25 evidence-packaging repair: regression tests pinned directly
to the seven real failure cases disclosed from a live HermesCliProvider
replay against real production stories (Alibaba share placement, SHEIN
underwriter list, NetEase Youdao, NZ social-media ban, Nvidia/Perplexity
hedge-dropping). Each test reproduces the specific gap that caused a
correct, source-grounded model output to be rejected as untraceable, and
proves it is now packaged/validated correctly -- without weakening
rejection of output that is genuinely unsupported."""

from __future__ import annotations

from news_intel.models import EventEnrichment
from news_intel.pipeline.editorial_evidence import build_event_input
from news_intel.pipeline.editorial_validation import _numbers_in, validate_enrichment
from news_intel.pipeline.entity_aliases import alias_variants

# --- Alibaba: "710M shares" (bare magnitude, no $ sign) -------------------


def test_numbers_in_resolves_bare_english_magnitude_suffix():
    assert 710_000_000.0 in _numbers_in("it plans to offer 710M shares at a discount")
    assert 710_000_000.0 in _numbers_in("it plans to offer 710 million shares at a discount")
    # the bare, unscaled digit must NOT also be contributed as a second,
    # wrong number (mirrors the existing Chinese magnitude-suffix guard)
    assert 710.0 not in _numbers_in("it plans to offer 710M shares at a discount")


def test_evidence_packet_offers_bare_magnitude_share_count_as_a_fact():
    primary = {
        "event_id": "E-1", "title": "Alibaba plans a follow-on share offering",
        "excerpt": "Alibaba plans to offer 710M shares at a 3.6% discount to Friday's close.",
        "entities": ["Alibaba"],
    }
    evidence = build_event_input(primary, None, "US / China / World", ["Alibaba"], edition_id="en_global")
    assert any(f.value == 710_000_000.0 for f in evidence.facts)


def test_validate_enrichment_accepts_a_bare_magnitude_number_from_source_text():
    primary = {
        "event_id": "E-1", "title": "Alibaba plans a follow-on share offering",
        "excerpt": "Alibaba plans to offer 710M shares at a discount to Friday's close.",
        "entities": ["Alibaba"],
    }
    evidence = build_event_input(primary, None, "US / China / World", ["Alibaba"], edition_id="en_global")
    fact_ids = [f.id for f in evidence.facts]
    e = EventEnrichment(
        event_id="E-1", category="US / China / World", english_headline="Alibaba share offering",
        what_happened="Alibaba plans to offer 710,000,000 shares to fund AI investments.",
        why_it_matters="The offering funds Alibaba's continued AI infrastructure investment.",
        chinese_headline="阿里巴巴计划配股", chinese_what_happened="阿里巴巴计划发行7.1亿股股票。",
        chinese_takeaway="此次配股旨在为阿里巴巴的AI投资提供资金支持。",
        significance_proposal=60, market_impact_proposal=50,
        supporting_source_ids=["s0"], cited_fact_ids=fact_ids, entities=["Alibaba"],
    )
    result = validate_enrichment(e, evidence)
    assert result.ok, result.reasons


# --- SHEIN: Chinese source, English enrichment naming banks in English ----


def test_entity_alias_variants_cover_known_financial_institutions():
    assert "高盛" in alias_variants("Goldman Sachs")
    assert "goldman sachs" in alias_variants("高盛")
    assert "摩根大通" in alias_variants("JPMorgan")


def test_entity_alias_variants_cover_luxshare():
    """2026-08-25: a real live-model replay showed 立讯精密 (a zh_cn source's
    own entity name) rejected against the model's own English rendering
    ("Luxshare Precision"/"Luxshare Limited") across three separate
    digests -- a recurring, not one-off, alias gap."""
    assert "立讯精密" in alias_variants("Luxshare Precision")
    assert "立讯精密" in alias_variants("Luxshare Limited")
    assert "luxshare precision" in alias_variants("立讯精密")


def test_validate_enrichment_accepts_english_bank_name_from_chinese_source():
    primary = {
        "event_id": "E-2", "title": "希音启动全球发售",
        "excerpt": "希音正式开启全球发售。高盛、摩根士丹利及摩根大通担任联席保荐人。",
        "entities": ["希音"],
    }
    evidence = build_event_input(primary, None, "Markets & Economy", ["希音"], edition_id="en_global")
    fact_ids = [f.id for f in evidence.facts]
    e = EventEnrichment(
        event_id="E-2", category="Markets & Economy", english_headline="SHEIN launches global offering",
        what_happened="SHEIN has launched its global share offering.",
        why_it_matters="Goldman Sachs, Morgan Stanley, and JPMorgan are joint sponsors of the offering.",
        chinese_headline="希音启动全球发售", chinese_what_happened="希音正式开启全球发售。",
        chinese_takeaway="高盛、摩根士丹利及摩根大通担任联席保荐人。",
        significance_proposal=55, market_impact_proposal=45,
        supporting_source_ids=["s0"], cited_fact_ids=fact_ids, entities=["希音", "Goldman Sachs"],
    )
    result = validate_enrichment(e, evidence)
    assert result.ok, result.reasons


def test_validate_enrichment_still_rejects_a_genuinely_fabricated_entity():
    """The alias table must not become a loophole -- a name with no
    relationship to any offered fact or source text is still rejected."""
    primary = {
        "event_id": "E-2", "title": "希音启动全球发售",
        "excerpt": "希音正式开启全球发售。高盛、摩根士丹利及摩根大通担任联席保荐人。",
        "entities": ["希音"],
    }
    evidence = build_event_input(primary, None, "Markets & Economy", ["希音"], edition_id="en_global")
    fact_ids = [f.id for f in evidence.facts]
    e = EventEnrichment(
        event_id="E-2", category="Markets & Economy", english_headline="SHEIN launches global offering",
        what_happened="SHEIN has launched its global share offering.",
        why_it_matters="Fabricated Capital Partners is a joint sponsor of the offering.",
        chinese_headline="希音启动全球发售", chinese_what_happened="希音正式开启全球发售。",
        chinese_takeaway="虚构资本是此次发售的联席保荐人。",
        significance_proposal=55, market_impact_proposal=45,
        supporting_source_ids=["s0"], cited_fact_ids=fact_ids, entities=["希音", "Fabricated Capital Partners"],
    )
    result = validate_enrichment(e, evidence)
    assert not result.ok
    assert any("Fabricated Capital Partners" in r for r in result.reasons)


# --- broader actor packaging: full entities list + capitalized-sequence ---


def test_evidence_packet_packages_every_offered_entity_not_just_principal():
    primary = {
        "event_id": "E-3", "title": "Nvidia in talks to invest in Perplexity",
        "excerpt": "Nvidia is in talks to invest in Perplexity as part of a new equity round.",
        "entities": ["Nvidia", "Perplexity"],
    }
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Nvidia", "Perplexity"],
                                  edition_id="en_global")
    actor_texts = {f.text for f in evidence.facts if f.kind == "actor"}
    assert "Nvidia" in actor_texts
    assert "Perplexity" in actor_texts


def test_evidence_packet_extracts_additional_capitalized_org_names_from_text():
    primary = {
        "event_id": "E-4", "title": "Alibaba plans a follow-on share offering",
        "excerpt": "Alibaba's offering will be arranged via the Hong Kong Stock Exchange, sources say.",
        "entities": ["Alibaba"],
    }
    evidence = build_event_input(primary, None, "US / China / World", ["Alibaba"], edition_id="en_global")
    actor_texts = {f.text for f in evidence.facts if f.kind == "actor"}
    assert "Hong Kong Stock Exchange" in actor_texts


def test_evidence_packet_extracts_chinese_organization_names_by_suffix():
    primary = {
        "event_id": "E-5", "title": "希音启动全球发售",
        "excerpt": "希音正式开启全球发售，泰康人寿担任基石投资者。",
        "entities": ["希音"],
    }
    evidence = build_event_input(primary, None, "Markets & Economy", ["希音"], edition_id="zh_cn")
    actor_texts = {f.text for f in evidence.facts if f.kind == "actor"}
    assert any("泰康人寿" in t for t in actor_texts)
