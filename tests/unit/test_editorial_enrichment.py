"""2026-08-24: the bounded per-story LLM editorial layer. All tests use a
FakeProvider -- never a real network call to Hermes or anywhere else, per
this repo's established "reproduce, don't call live services in tests"
practice and the explicit boundary against inspecting/using real Hermes
credentials in this environment."""

from __future__ import annotations

import inspect

from news_intel.llm.errors import LLMUnavailableError
from news_intel.models import EventEnrichment, EventInput
from news_intel.pipeline.editorial_enrichment import EditorialResult, enrich_story
from news_intel.pipeline.editorial_evidence import build_event_input
from news_intel.pipeline.editorial_validation import validate_enrichment


def _primary(title="Starcloud raises $250 million for orbital data centers", excerpt=None, edition_id="en_global"):
    excerpt = excerpt or (
        "Starcloud, a startup developing satellites that can perform AI inference in orbit, "
        "raised $250 million."
    )
    return {
        "event_id": "E-1", "title": title, "excerpt": excerpt, "category": "AI & Semiconductors",
        "entities": ["Starcloud"], "edition_id": edition_id,
        "sources": [{"publisher": "TechCrunch", "excerpt": excerpt, "url": "https://techcrunch.test/x",
                     "published_at": "2026-08-21T15:00:00+00:00"}],
    }


def _good_enrichment(evidence: EventInput) -> EventEnrichment:
    fact_ids = [f.id for f in evidence.facts]
    return EventEnrichment(
        event_id=evidence.event_id, category=evidence.category_candidates[0] if evidence.category_candidates else "",
        english_headline="Starcloud raises $250M for orbital AI inference.",
        what_happened="Starcloud raised $250 million to expand orbital AI data centers.",
        why_it_matters=(
            "The funding extends Starcloud's runway for orbital AI inference amid constrained launch capacity."
        ),
        chinese_headline="Starcloud融资2.5亿美元",
        chinese_takeaway="Starcloud获得2.5亿美元融资，用于轨道AI数据中心扩张。",
        significance_proposal=70, market_impact_proposal=50,
        supporting_source_ids=[s["id"] for s in evidence.sources], cited_fact_ids=fact_ids,
        entities=["Starcloud"],
    )


def _bad_enrichment_unsupported_number(evidence: EventInput) -> EventEnrichment:
    e = _good_enrichment(evidence)
    return e.model_copy(update={"why_it_matters": "NVIDIA acquired Starcloud for $9 billion in cash."})


def _bad_enrichment_unsupported_name(evidence: EventInput) -> EventEnrichment:
    e = _good_enrichment(evidence)
    return e.model_copy(update={"entities": ["Starcloud", "Fabricated Rival Corp"]})


def _bad_enrichment_generic(evidence: EventInput) -> EventEnrichment:
    e = _good_enrichment(evidence)
    return e.model_copy(update={
        "why_it_matters": "This development involving Starcloud ranks among today's most material developments.",
    })


class _FakeProvider:
    """Deterministic, in-memory stand-in -- never touches a network. Queue
    a sequence of responses (an EventEnrichment, or an Exception instance
    to raise) to simulate exactly one attempt each call."""

    model = "fake-mimo-test"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[EventInput] = []

    def enrich_events(self, events):
        self.calls.append(events[0])
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return [response]


# --- editorial_evidence ------------------------------------------------


def test_build_event_input_extracts_actor_and_numeric_facts():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    assert evidence.event_id == "E-1"
    assert any(f.kind == "actor" and f.text == "Starcloud" for f in evidence.facts)
    assert any(f.kind == "numeric" and f.value == 250_000_000.0 for f in evidence.facts)
    assert evidence.category_candidates == ["AI & Semiconductors"]


def test_build_event_input_flags_uncertainty_when_present():
    primary = _primary(
        title="Sources: Anthropic's bankers said the company could raise $100B+",
        excerpt="Anthropic's bankers said the company could value it at $2 trillion in discussions with investors.",
    )
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Anthropic"], edition_id="en_global")
    assert any(f.kind == "uncertainty" for f in evidence.facts)


# --- editorial_validation ------------------------------------------------


def test_validate_enrichment_accepts_grounded_output():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    result = validate_enrichment(_good_enrichment(evidence), evidence)
    assert result.ok, result.reasons


def test_validate_enrichment_rejects_unsupported_number():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    result = validate_enrichment(_bad_enrichment_unsupported_number(evidence), evidence)
    assert not result.ok
    assert any("9" in r or "traceable" in r for r in result.reasons)


def test_validate_enrichment_rejects_unsupported_entity_name():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    result = validate_enrichment(_bad_enrichment_unsupported_name(evidence), evidence)
    assert not result.ok
    assert any("Fabricated Rival Corp" in r for r in result.reasons)


def test_validate_enrichment_rejects_banned_generic_language():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    result = validate_enrichment(_bad_enrichment_generic(evidence), evidence)
    assert not result.ok
    assert any("banned generic phrase" in r for r in result.reasons)


def test_validate_enrichment_rejects_empty_cited_facts():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    e = _good_enrichment(evidence).model_copy(update={"cited_fact_ids": []})
    result = validate_enrichment(e, evidence)
    assert not result.ok
    assert any("cited_fact_ids is empty" in r for r in result.reasons)


def test_validate_enrichment_rejects_fabricated_fact_reference():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    e = _good_enrichment(evidence).model_copy(update={"cited_fact_ids": ["f999-does-not-exist"]})
    result = validate_enrichment(e, evidence)
    assert not result.ok
    assert any("never offered" in r for r in result.reasons)


def test_validate_enrichment_rejects_category_swap():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    e = _good_enrichment(evidence).model_copy(update={"category": "Politics"})
    result = validate_enrichment(e, evidence)
    assert not result.ok
    assert any("category" in r for r in result.reasons)


def test_validate_enrichment_rejects_dangling_clause():
    evidence = build_event_input(_primary(), None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    e = _good_enrichment(evidence).model_copy(
        update={"why_it_matters": "The funding matters because it shows that"}
    )
    result = validate_enrichment(e, evidence)
    assert not result.ok


def test_validate_enrichment_requires_hedge_when_uncertainty_flagged():
    primary = _primary(
        title="Sources: Anthropic's bankers said the company could raise $100B+",
        excerpt="Anthropic's bankers said the company could value it at $2 trillion in discussions with investors.",
    )
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Anthropic"], edition_id="en_global")
    e = EventEnrichment(
        event_id="E-1", category="AI & Semiconductors", english_headline="Anthropic valued at $2 trillion",
        what_happened="Anthropic is valued at $2 trillion.",
        why_it_matters="Anthropic is valued at $2 trillion, a huge milestone.",
        chinese_headline="Anthropic估值2万亿美元", chinese_takeaway="Anthropic估值2万亿美元。",
        significance_proposal=80, market_impact_proposal=60,
        supporting_source_ids=[s["id"] for s in evidence.sources], cited_fact_ids=[f.id for f in evidence.facts],
        entities=["Anthropic"],
    )
    result = validate_enrichment(e, evidence)
    assert not result.ok
    assert any("prospective" in r for r in result.reasons)


# --- editorial_enrichment orchestration -----------------------------------


def test_enrich_story_with_no_provider_uses_source_grounded_fallback():
    result = enrich_story(_primary(), None, "AI & Semiconductors", ["Starcloud"], provider=None)
    assert result.source == "deterministic_fallback"
    assert result.why_it_matters is None  # omitted, never a generic template
    assert result.chinese_takeaway is None
    assert result.what_happened is not None
    assert "Starcloud" in result.what_happened


def test_enrich_story_accepts_valid_first_attempt():
    primary = _primary()
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    provider = _FakeProvider([_good_enrichment(evidence)])
    result = enrich_story(primary, None, "AI & Semiconductors", ["Starcloud"], provider=provider)
    assert result.source == "llm_validated"
    assert result.validated is True
    assert result.retry_count == 0
    assert result.why_it_matters is not None
    assert len(provider.calls) == 1


def test_enrich_story_retries_once_with_validation_errors_then_succeeds():
    primary = _primary()
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    bad = _bad_enrichment_generic(evidence)
    good = _good_enrichment(evidence)
    provider = _FakeProvider([bad, good])
    result = enrich_story(primary, None, "AI & Semiconductors", ["Starcloud"], provider=provider)
    assert result.source == "llm_validated"
    assert result.retry_count == 1
    assert len(provider.calls) == 2
    # the retry prompt carries the rejection reason forward
    assert "PRIOR ATTEMPT REJECTED" in provider.calls[1].candidate_title


def test_enrich_story_falls_back_after_repeated_invalid_output():
    primary = _primary()
    evidence = build_event_input(primary, None, "AI & Semiconductors", ["Starcloud"], edition_id="en_global")
    bad1 = _bad_enrichment_generic(evidence)
    bad2 = _bad_enrichment_unsupported_number(evidence)
    provider = _FakeProvider([bad1, bad2])
    result = enrich_story(primary, None, "AI & Semiconductors", ["Starcloud"], provider=provider)
    assert result.source == "deterministic_fallback"
    assert result.why_it_matters is None
    assert "validation failed" in result.fallback_reason
    assert result.retry_count == 2  # both attempts (original + the one retry) failed validation


def test_enrich_story_timeout_or_provider_error_produces_safe_fallback():
    provider = _FakeProvider([LLMUnavailableError("simulated timeout")])
    result = enrich_story(_primary(), None, "AI & Semiconductors", ["Starcloud"], provider=provider)
    assert result.source == "deterministic_fallback"
    assert result.why_it_matters is None
    assert "provider unavailable" in result.fallback_reason
    assert result.what_happened is not None  # still source-grounded, not empty


def test_enrich_story_never_omits_deterministic_floor_entirely():
    """Even total LLM failure must never leave a story with NO what_happened
    at all -- the deterministic floor is unconditional."""
    provider = _FakeProvider([LLMUnavailableError("x"), LLMUnavailableError("x")])
    result = enrich_story(_primary(), None, "AI & Semiconductors", ["Starcloud"], provider=provider)
    assert result.what_happened


def test_enrich_story_works_for_zh_cn_edition_with_no_provider():
    primary = {
        "event_id": "E-2", "title": "长江存储冲刺科创板", "excerpt": "长江存储 IPO 审核状态已变更为已受理。",
        "category": "Markets & Economy", "entities": ["长江存储"], "edition_id": "zh_cn",
        "sources": [{"publisher": "IT之家", "excerpt": "长江存储 IPO 审核状态已变更为已受理。",
                     "url": "https://ithome.test/x", "published_at": "2026-08-22"}],
    }
    result = enrich_story(primary, None, "Markets & Economy", ["长江存储"], edition_id="zh_cn", provider=None)
    assert result.source == "deterministic_fallback"
    assert result.what_happened is not None


# --- structural safety: the editorial layer cannot itself send Telegram ---


def test_enrich_story_signature_has_no_telegram_capability():
    """Structural proof, not just a runtime check: enrich_story() cannot be
    handed a chat_id, sender, or delivery-authorization object at all --
    the parameter simply does not exist."""
    params = set(inspect.signature(enrich_story).parameters)
    assert "chat_id" not in params
    assert "sender" not in params
    assert "authorization" not in params


def test_editorial_result_carries_no_delivery_state():
    result = EditorialResult(what_happened="x", why_it_matters=None, chinese_takeaway=None, category=None,
                              source="deterministic_fallback")
    assert not hasattr(result, "sent")
    assert not hasattr(result, "chat_id")


def test_build_bounded_provider_returns_none_when_unconfigured(config):
    from news_intel.pipeline.editorial_enrichment import build_bounded_provider

    assert config.secrets.llm_base_url in (None, "")
    assert build_bounded_provider(config) is None
