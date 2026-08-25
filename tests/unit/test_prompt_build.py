"""2026-08-24: the shared enrich_event prompt builder. Proves the fix for
the gap where EventInput.facts/category_candidates were collected but
never rendered into the prompt text -- without this, no real provider
could ever produce a legitimate, non-empty, non-fabricated
cited_fact_ids."""

from __future__ import annotations

from news_intel.llm.prompt_build import build_enrich_event_prompt, render_facts_block
from news_intel.models import EventInput, EvidenceFact


def _evidence(facts=None, category_candidates=None):
    if category_candidates is None:
        category_candidates = ["AI & Semiconductors"]
    return EventInput(
        event_id="E-1",
        candidate_title="Starcloud raises $250 million",
        sources=[{"id": "s0", "publisher": "TechCrunch", "excerpt": "Starcloud raised $250 million.",
                  "published_at": "2026-08-21T15:00:00+00:00"}],
        edition_id="en_global",
        category_candidates=category_candidates,
        facts=facts or [],
    )


def test_render_facts_block_empty():
    assert render_facts_block([]) == "(none offered)"


def test_render_facts_block_includes_id_and_text():
    facts = [EvidenceFact(id="f1", kind="numeric", text="$250,000,000", value=250_000_000.0, unit="USD",
                           role="financing")]
    block = render_facts_block(facts)
    assert 'id="f1"' in block
    assert 'kind="numeric"' in block
    assert "$250,000,000" in block
    assert 'role="financing"' in block


def test_build_enrich_event_prompt_includes_fact_ids():
    facts = [EvidenceFact(id="f1", kind="actor", text="Starcloud"), EvidenceFact(id="f2", kind="numeric",
                                                                                  text="$250M", value=250_000_000.0)]
    evidence = _evidence(facts=facts)
    prompt = build_enrich_event_prompt(evidence)
    assert "f1" in prompt
    assert "f2" in prompt
    assert "cited_fact_ids" in prompt


def test_build_enrich_event_prompt_includes_category_candidates():
    evidence = _evidence(category_candidates=["Markets & Economy"])
    prompt = build_enrich_event_prompt(evidence)
    assert "Markets & Economy" in prompt


def test_build_enrich_event_prompt_no_candidates_shows_placeholder():
    evidence = _evidence(category_candidates=[])
    prompt = build_enrich_event_prompt(evidence)
    assert "(none provided)" in prompt


def test_build_enrich_event_prompt_includes_sources():
    evidence = _evidence()
    prompt = build_enrich_event_prompt(evidence)
    assert "Starcloud raised $250 million." in prompt
    assert '<source id="s0"' in prompt
