"""Bounded Hermes editorial layer (2026-08-07 content-quality remediation):
candidate packet construction, the strict deterministic safety envelope
around Hermes's output, and the review-only fallback when Hermes is
unavailable or returns anything invalid. Not wired into delivery -- these
tests never touch Telegram/deliveries and mock the Hermes subprocess call
(real end-to-end validation against Hermes is documented separately in the
incident report, not re-run on every test invocation)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

from news_intel.pipeline.hermes_editorial import (
    MAX_MESSAGE_CHARS,
    Candidate,
    HermesCallResult,
    ResolvedItem,
    build_candidate_packet,
    build_review_only_fallback,
    call_hermes,
    compose_hermes_assisted_digest,
    render_hermes_digest,
    render_hermes_prompt,
    validate_hermes_response,
)
from tests.conftest import insert_source_row

_NOW = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
_WINDOW_START = datetime(2026, 8, 7, 5, 0, tzinfo=UTC)
_WINDOW_END = datetime(2026, 8, 8, 5, 0, tzinfo=UTC)


def _seed_event(
    conn, *, event_id, title, category, score, now=_NOW, archive_only=0, url="https://x.test/0", excerpt="excerpt",
):
    source_id = f"src_{event_id}"
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, category, first_seen_at, last_updated_at, "
        "published_anchor_at, confirmation_state, final_score, archive_only) "
        "VALUES (?, ?, ?, ?, ?, ?, 'official', ?, ?)",
        (event_id, title, category, now.isoformat(), now.isoformat(), now.isoformat(), score, archive_only),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
        (source_id, f"{event_id}-0", url, f"{event_id}-hash", title, title.lower(), excerpt,
         now.isoformat(), f"{event_id}-content", source_id),
    )
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, cur.lastrowid),
    )


def _candidate(event_id="E-1", category="AI & Semiconductors", score=90.0) -> Candidate:
    return Candidate(
        event_id=event_id, title=f"Title for {event_id}", excerpt="Some excerpt.", updated_at=_NOW.isoformat(),
        category=category, score=score, source_name="Test Source", source_url=f"https://x.test/{event_id}",
    )


def test_candidate_packet_excludes_noise_and_archive_but_includes_unclassified(config, conn):
    _seed_event(conn, event_id="E-NOISE", title="Noise item", category="noise", score=90)
    _seed_event(
        conn, event_id="E-ARCHIVE", title="Archived item", category="AI & Semiconductors", score=90, archive_only=1
    )
    _seed_event(conn, event_id="E-UNCLASSIFIED", title="Unclassified item", category="Unclassified", score=40)
    _seed_event(conn, event_id="E-GOOD", title="Good item", category="Markets & Economy", score=60)

    packet = build_candidate_packet(conn, window_start=_WINDOW_START, window_end=_WINDOW_END)
    ids = {c.event_id for c in packet}

    assert "E-NOISE" not in ids
    assert "E-ARCHIVE" not in ids
    assert "E-UNCLASSIFIED" in ids  # deliberately included -- Hermes may rescue/recategorize it
    assert "E-GOOD" in ids


def test_candidate_packet_excludes_events_outside_window(config, conn):
    outside = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)
    _seed_event(conn, event_id="E-OLD", title="Old item", category="AI & Semiconductors", score=90, now=outside)
    packet = build_candidate_packet(conn, window_start=_WINDOW_START, window_end=_WINDOW_END)
    assert "E-OLD" not in {c.event_id for c in packet}


def test_render_hermes_prompt_contains_event_ids_and_instructs_no_url():
    candidates = [_candidate("E-1"), _candidate("E-2", category="Markets & Economy")]
    prompt = render_hermes_prompt(candidates)
    assert "E-1" in prompt
    assert "E-2" in prompt
    assert "Never include a URL or source name" in prompt
    assert "REJECT marketing" in prompt


def test_call_hermes_uses_validated_oneshot_interface_with_empty_toolset():
    with patch("news_intel.pipeline.hermes_editorial.subprocess.run") as mocked:
        mocked.return_value.returncode = 0
        mocked.return_value.stdout = '{"selected": []}'
        mocked.return_value.stderr = ""
        call_hermes("a prompt", timeout_seconds=5.0)

    args, kwargs = mocked.call_args
    assert args[0] == ["hermes", "-z", "a prompt", "-t", ""]
    assert kwargs["timeout"] == 5.0


def test_validate_rejects_event_id_not_in_candidate_set():
    candidates = [_candidate("E-1"), _candidate("E-2"), _candidate("E-3"), _candidate("E-4")]
    raw = json.dumps({"selected": [
        {"event_id": "E-999", "tier": "top3", "category": "AI & Semiconductors",
         "english_summary": "x", "why_it_matters": "y", "chinese_takeaway": "z"},
    ]})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("not in the supplied candidate set" in e for e in outcome.errors)


def test_validate_rejects_duplicate_event_id():
    candidates = [_candidate(f"E-{i}") for i in range(1, 6)]
    item = {"event_id": "E-1", "tier": "quick", "category": "AI & Semiconductors",
            "english_summary": "x", "chinese_takeaway": "z"}
    raw = json.dumps({"selected": [item, item, item, item]})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("duplicate event_id" in e for e in outcome.errors)


def test_validate_rejects_category_not_in_allowed_set():
    """Hermes literally cannot mark a selected item 'Unclassified' -- it is
    not in the allowed category enum, so schema validation itself rejects it."""
    candidates = [_candidate("E-1")]
    raw = json.dumps({"selected": [
        {"event_id": "E-1", "tier": "top3", "category": "Unclassified",
         "english_summary": "x", "why_it_matters": "y", "chinese_takeaway": "z"},
    ]})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("schema validation" in e for e in outcome.errors)


def test_validate_rejects_too_few_selected():
    candidates = [_candidate(f"E-{i}") for i in range(1, 10)]
    raw = json.dumps({"selected": [
        {"event_id": "E-1", "tier": "top3", "category": "AI & Semiconductors",
         "english_summary": "x", "why_it_matters": "y", "chinese_takeaway": "z"},
    ]})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("outside the required" in e for e in outcome.errors)


def test_validate_rejects_too_many_top3():
    candidates = [_candidate(f"E-{i}") for i in range(1, 10)]
    selected = [
        {"event_id": f"E-{i}", "tier": "top3", "category": "AI & Semiconductors",
         "english_summary": "x", "why_it_matters": "y", "chinese_takeaway": "z"}
        for i in range(1, 5)
    ]
    raw = json.dumps({"selected": selected})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("exceeds the max" in e for e in outcome.errors)


def test_validate_rejects_top3_without_why_it_matters():
    candidates = [_candidate(f"E-{i}") for i in range(1, 6)]
    selected = [
        {"event_id": "E-1", "tier": "top3", "category": "AI & Semiconductors",
         "english_summary": "x", "chinese_takeaway": "z"},
        *[{"event_id": f"E-{i}", "tier": "quick", "category": "AI & Semiconductors",
           "english_summary": "x", "chinese_takeaway": "z"} for i in range(2, 5)],
    ]
    raw = json.dumps({"selected": selected})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is False
    assert any("no why_it_matters" in e for e in outcome.errors)


def test_validate_resolves_url_and_title_from_candidate_never_from_hermes():
    candidates = [_candidate(f"E-{i}") for i in range(1, 5)]
    selected = [
        {"event_id": f"E-{i}", "tier": "quick", "category": "AI & Semiconductors",
         "english_summary": "x", "chinese_takeaway": "z"} for i in range(1, 5)
    ]
    raw = json.dumps({"selected": selected})
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is True
    for item, candidate in zip(outcome.items, candidates, strict=False):
        assert item.source_url == candidate.source_url
        assert item.title == candidate.title


def test_validate_strips_code_fences_defensively():
    candidates = [_candidate(f"E-{i}") for i in range(1, 5)]
    selected = [
        {"event_id": f"E-{i}", "tier": "quick", "category": "AI & Semiconductors",
         "english_summary": "x", "chinese_takeaway": "z"} for i in range(1, 5)
    ]
    raw = "```json\n" + json.dumps({"selected": selected}) + "\n```"
    outcome = validate_hermes_response(raw, candidates)
    assert outcome.ok is True


def test_render_hermes_digest_matches_required_format():
    # Rendering is tested directly against ResolvedItem, independent of
    # validate_hermes_response's own MIN_SELECTED/MAX_SELECTED count rules
    # (covered separately) -- this test is only about output formatting.
    items = [
        ResolvedItem(
            event_id="E-1", tier="top3", category="AI & Semiconductors",
            english_summary="A thing happened.", why_it_matters="It matters.", chinese_takeaway="很重要。",
            title="Title for E-1", source_url="https://x.test/E-1", source_name="Test Source",
        ),
        ResolvedItem(
            event_id="E-2", tier="quick", category="Markets & Economy",
            english_summary="x", why_it_matters=None, chinese_takeaway="简报。",
            title="Title for E-2", source_url="https://x.test/E-2", source_name="Test Source",
        ),
    ]

    rendered = render_hermes_digest("Aug 7", items)
    assert "TOP 3" in rendered
    assert "QUICK BRIEFS" in rendered
    assert "English summary: A thing happened." in rendered
    assert "Why it matters: It matters." in rendered
    assert "中文要点：很重要。" in rendered
    assert "Source: https://x.test/E-1" in rendered
    assert "- Title for E-2 — 简报。 https://x.test/E-2" in rendered


def test_compose_falls_back_to_review_only_on_hermes_error(config, conn):
    _seed_event(conn, event_id="E-A", title="A", category="AI & Semiconductors", score=90)
    _seed_event(conn, event_id="E-B", title="B", category="Markets & Economy", score=60)

    with patch(
        "news_intel.pipeline.hermes_editorial.call_hermes",
        return_value=HermesCallResult("", 1.0, -1, False, "hermes CLI not found"),
    ):
        outcome = compose_hermes_assisted_digest(
            conn, window_start=_WINDOW_START, window_end=_WINDOW_END, date_label="Aug 7"
        )

    assert outcome.fallback_used is True
    assert "REVIEW ONLY" in outcome.rendered_text
    assert "hermes CLI not found" in " ".join(outcome.validation_errors)


def test_compose_falls_back_to_review_only_on_invalid_json(config, conn):
    _seed_event(conn, event_id="E-A", title="A", category="AI & Semiconductors", score=90)

    with patch(
        "news_intel.pipeline.hermes_editorial.call_hermes",
        return_value=HermesCallResult("not json at all", 2.0, 0, False, None),
    ):
        outcome = compose_hermes_assisted_digest(
            conn, window_start=_WINDOW_START, window_end=_WINDOW_END, date_label="Aug 7"
        )

    assert outcome.fallback_used is True
    assert "REVIEW ONLY" in outcome.rendered_text
    assert "not valid JSON" in " ".join(outcome.validation_errors)


def test_compose_falls_back_to_review_only_on_oversized_render(config, conn):
    for i in range(8):
        _seed_event(conn, event_id=f"E-{i}", title=f"Title {i}", category="AI & Semiconductors", score=90 - i)

    huge_summary = "x" * 500  # the schema max, deliberately used on every item to blow past MAX_MESSAGE_CHARS
    selected = [
        {"event_id": f"E-{i}", "tier": "top3" if i < 3 else "quick", "category": "AI & Semiconductors",
         "english_summary": huge_summary, "why_it_matters": ("y" * 300) if i < 3 else None,
         "chinese_takeaway": "很" * 200}
        for i in range(8)
    ]
    raw = json.dumps({"selected": selected})

    with patch(
        "news_intel.pipeline.hermes_editorial.call_hermes",
        return_value=HermesCallResult(raw, 3.0, 0, False, None),
    ):
        outcome = compose_hermes_assisted_digest(
            conn, window_start=_WINDOW_START, window_end=_WINDOW_END, date_label="Aug 7"
        )

    assert outcome.fallback_used is True
    assert "REVIEW ONLY" in outcome.rendered_text
    assert outcome.character_count <= MAX_MESSAGE_CHARS  # the fallback text itself, not the rejected digest
    assert any("exceeds" in e for e in outcome.validation_errors)


def test_compose_succeeds_with_valid_hermes_response(config, conn):
    for i in range(6):
        cat = "AI & Semiconductors" if i % 2 == 0 else "Markets & Economy"
        _seed_event(conn, event_id=f"E-{i}", title=f"Title {i}", category=cat, score=90 - i)

    selected = [
        {"event_id": f"E-{i}", "tier": "top3" if i < 2 else "quick",
         "category": "AI & Semiconductors" if i % 2 == 0 else "Markets & Economy",
         "english_summary": f"Summary {i}.", "why_it_matters": "Matters." if i < 2 else None,
         "chinese_takeaway": "要点。"}
        for i in range(6)
    ]
    raw = json.dumps({"selected": selected})

    with patch(
        "news_intel.pipeline.hermes_editorial.call_hermes",
        return_value=HermesCallResult(raw, 4.0, 0, False, None),
    ):
        outcome = compose_hermes_assisted_digest(
            conn, window_start=_WINDOW_START, window_end=_WINDOW_END, date_label="Aug 7"
        )

    assert outcome.fallback_used is False
    assert outcome.selected_count == 6
    assert "REVIEW ONLY" not in outcome.rendered_text
    assert "TOP 3" in outcome.rendered_text
    assert "QUICK BRIEFS" in outcome.rendered_text


def test_review_only_fallback_is_clearly_labeled_and_not_a_digest():
    candidates = [_candidate("E-1"), _candidate("E-2")]
    text = build_review_only_fallback("Aug 7", candidates, ["Hermes unavailable"])
    assert text.startswith("[REVIEW ONLY")
    assert "TOP 3" not in text
    assert "Hermes unavailable" in text
