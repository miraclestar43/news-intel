from news_intel.llm.fallback import FALLBACK_NOTICE, fallback_daily_digest, fallback_enrich
from news_intel.models import DailyDigestInput, EventInput


def test_fallback_daily_digest_includes_notice_and_events():
    inputs = DailyDigestInput(
        date_label="Aug 1",
        top3_candidates=[
            {
                "title": "NVIDIA export control chip rule",
                "category": "semiconductor",
                "excerpt": "New export control rule affects AI chip shipments.",
                "sources": [{"publisher": "NVIDIA Newsroom"}],
            }
        ],
        quick_brief_candidates=[
            {"title": "Fed holds rates steady", "category": "macro"},
        ],
        calendar_candidates=[{"title": "CPI release — August"}],
    )
    out = fallback_daily_digest(inputs, "Aug 1")

    assert out.fallback_used is True
    assert FALLBACK_NOTICE in out.rendered_text
    assert "NVIDIA export control chip rule" in out.rendered_text
    assert "NVIDIA Newsroom" in out.rendered_text
    assert "Fed holds rates steady" in out.rendered_text
    assert "CPI release" in out.rendered_text


def test_fallback_daily_digest_never_pads_empty_sections():
    inputs = DailyDigestInput(
        date_label="Aug 1", top3_candidates=[], quick_brief_candidates=[], calendar_candidates=[]
    )
    out = fallback_daily_digest(inputs, "Aug 1")
    assert "QUICK BRIEFS" not in out.rendered_text
    assert "TOMORROW TO WATCH" not in out.rendered_text


def test_fallback_enrich_never_invents_facts():
    event = EventInput(
        event_id="E-20260731-0001",
        candidate_title="Some headline",
        sources=[{"id": "S1", "excerpt": "Original excerpt text."}],
    )
    enrichment = fallback_enrich(event)
    assert enrichment.event_id == "E-20260731-0001"
    assert enrichment.what_happened == "Original excerpt text."
    assert enrichment.supporting_source_ids == ["S1"]
    assert enrichment.significance_proposal == 0
