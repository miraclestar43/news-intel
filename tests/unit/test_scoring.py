from datetime import UTC, datetime

from news_intel.models import Severity
from news_intel.pipeline.scoring import classify_severity_candidate, persist_score, score_event
from tests.conftest import insert_source_row


def _make_event_with_source(conn, title, excerpt, tier=0, origin_family="gov.test"):
    now = datetime.now(UTC).isoformat()
    event_id = "E-20260731-0099"
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, confirmation_state) "
        "VALUES (?, ?, ?, ?, 'unverified')",
        (event_id, title, now, now),
    )
    insert_source_row(conn, "src1", tier)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
        "VALUES ('src1', 'ext1', 'https://x.test/1', 'h1', ?, ?, ?, 'en', ?, 'ch1', ?)",
        (title, title.lower(), excerpt, now, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, raw_id),
    )
    return event_id


def test_ai_semiconductor_event_scores_higher_than_noise(config, conn):
    ai_event = _make_event_with_source(
        conn, "NVIDIA export control chip rule", "New export control rule affects AI chip shipments."
    )
    scores = score_event(conn, ai_event, config, is_new_event=True)
    weights = config.policy.scoring.weights.model_dump()
    ai_final = persist_score(conn, ai_event, scores, weights, config.policy.scoring.thresholds)

    conn.execute("DELETE FROM event_sources")
    conn.execute("DELETE FROM raw_items")
    conn.execute("DELETE FROM events")

    noise_event = _make_event_with_source(
        conn, "Celebrity shopping sale rumor", "A celebrity rumor about a shopping coupon sale."
    )
    noise_scores = score_event(conn, noise_event, config, is_new_event=True)
    noise_final = persist_score(conn, noise_event, noise_scores, weights, config.policy.scoring.thresholds)

    assert ai_final > noise_final
    assert noise_scores.noise_penalty == 100.0


def test_classify_severity_thresholds(config):
    t = config.policy.scoring.thresholds
    assert classify_severity_candidate(t.alert_candidate, t) == Severity.S2
    assert classify_severity_candidate(t.daily_quick_brief, t) == Severity.S1
    assert classify_severity_candidate(0, t) == Severity.S0


def test_significance_no_longer_plateaus_at_47_5_for_unverified_single_source(config, conn):
    """2026-08-12 scoring repair reproduction: before, EVERY single-source
    UNVERIFIED (tier != 0) event scored significance = 40 + 0 + 30*0.25 =
    47.5 no matter what it was about. A materially significant single-
    source story (regulatory action, large dollar figure) must now be able
    to score well above that fixed value."""
    event_id = _make_event_with_source(
        conn,
        "Government imposes new sanctions and export controls on major firm",
        "Regulators announced a $2 billion penalty and new export controls, effective immediately.",
        tier=3,
        origin_family="gdelt-style.test",
    )
    scores = score_event(conn, event_id, config, is_new_event=True)
    assert scores.confirmation_quality == 30  # still unverified -- confidence component unchanged
    assert scores.significance > 47.5 + 15  # materiality no longer pinned to the old plateau value


def test_significance_independent_of_source_tier_for_identical_text(config, conn):
    """Same text, same confirmation state, different source tier -> the
    2026-08-12 repair means significance must be identical (materiality
    has no has_t0 input); only confirmation_quality/corroboration may
    legitimately differ with source characteristics."""
    text = "NVIDIA today announced a new product update for developers."
    t0_event = _make_event_with_source(conn, text, text, tier=0, origin_family="a.test")
    t0_scores = score_event(conn, t0_event, config, is_new_event=True)

    conn.execute("DELETE FROM event_sources")
    conn.execute("DELETE FROM raw_items")
    conn.execute("DELETE FROM events")

    t3_event = _make_event_with_source(conn, text, text, tier=3, origin_family="b.test")
    t3_scores = score_event(conn, t3_event, config, is_new_event=True)

    assert t0_scores.significance == t3_scores.significance
