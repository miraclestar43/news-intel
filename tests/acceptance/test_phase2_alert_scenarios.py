"""The eight required deterministic alert fixture scenarios (design doc
Phase 2 acceptance list). Real Telegram sending is never exercised here --
only the deterministic eligibility/queueing state machine."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.adapters.base import CollectedItem
from news_intel.delivery.alert_formatter import render_alert
from news_intel.jobs.alerts import AlertActionResult, finalize_expired_merge_batches, process_event_for_alerts
from news_intel.models import ConfirmationState, Severity
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from tests.conftest import insert_source_row

PT = ZoneInfo("America/Los_Angeles")


def _deliver(conn, config, row, now):
    """Test helper for scenarios that aren't specifically about burst-merge
    timing: process one candidate, then force-resolve its S2 burst-merge
    window immediately (as if nothing else arrived to merge with it) so the
    test can assert on final delivery state without waiting out the real
    10-minute window. Scenarios that test merge timing itself call
    process_event_for_alerts / finalize_expired_merge_batches directly."""
    result = process_event_for_alerts(conn, config, row, now)
    if result.action == "pending_merge":
        finalize_expired_merge_batches(
            config, conn, now + timedelta(minutes=config.policy.alerts.burst_merge_minutes + 1)
        )
        final_state = conn.execute(
            "SELECT state FROM alert_queue WHERE event_id = ? ORDER BY id DESC LIMIT 1", (row["id"],)
        ).fetchone()["state"]
        result = AlertActionResult(
            result.event_id, final_state, severity=result.severity,
            is_statement_only=result.is_statement_only, message_hash=result.message_hash,
        )
    return result


def _seed_event(
    conn,
    *,
    event_id,
    title,
    excerpt,
    final_score=90,
    significance=85,
    market_impact=70,
    sources,  # list of (source_id, tier, origin_family)
    now,
    confirmation_state=None,
):
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, confirmation_state, "
        "final_score, significance, market_impact) VALUES (?, ?, ?, ?, 'unverified', ?, ?, ?)",
        (event_id, title, now.isoformat(), now.isoformat(), final_score, significance, market_impact),
    )
    for i, (source_id, tier, origin_family) in enumerate(sources):
        insert_source_row(conn, source_id, tier)
        cur = conn.execute(
            "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
            "normalized_title, summary_excerpt, language, fetched_at, content_hash, origin_family) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?)",
            (
                source_id, f"{event_id}-{i}", f"https://x.test/{event_id}-{i}", f"{event_id}-{i}",
                title, title.lower(), excerpt, now.isoformat(), f"{event_id}-{i}", origin_family,
            ),
        )
        raw_id = cur.lastrowid
        conn.execute(
            "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) "
            "VALUES (?, ?, ?, '[]')",
            (event_id, raw_id, int(i == 0)),
        )
    if confirmation_state is not None:
        conn.execute("UPDATE events SET confirmation_state = ? WHERE id = ?", (confirmation_state.value, event_id))
    else:
        update_confirmation_state(conn, event_id, now=now)


def test_scenario_1_official_export_control_plus_corroboration_sends_one_s2_alert(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-S1", title="BIS announces export control rule on AI chips",
        excerpt="The Bureau of Industry and Security issued a new export control rule on advanced AI chips.",
        sources=[("bis_gov", 0, "bis.gov"), ("wire_corroborating", 1, "wire.test")],
        now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-S1'").fetchone()

    result = _deliver(conn, config, row, now)

    assert result.action == "sent"
    assert result.severity == Severity.S2.value
    count = conn.execute("SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-S1'").fetchone()["n"]
    assert count == 1


def test_scenario_2_mirror_alone_is_unverified_candidate_no_push(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-S2", title="Trump posted about new China tariffs",
        excerpt="A mirror account posted that Trump said tariffs on China would increase.",
        sources=[("trumpdailyposts_mirror", 3, "trumpdailyposts.mirror")],
        now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-S2'").fetchone()
    assert ConfirmationState(row["confirmation_state"]) == ConfirmationState.UNVERIFIED

    result = process_event_for_alerts(conn, config, row, now)

    assert result.action == "not_eligible"
    assert "mirror" in result.reason.lower() or "t3" in result.reason.lower()
    count = conn.execute("SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-S2'").fetchone()["n"]
    assert count == 0


def test_scenario_3_verified_statement_without_policy_document_is_labeled_statement(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-S3", title="Trump said new tariffs on China would take effect",
        excerpt="Trump said in a statement that new tariffs on China would take effect next month.",
        sources=[("wire_a", 1, "wire-a.test"), ("wire_b", 1, "wire-b.test")],
        now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-S3'").fetchone()
    assert ConfirmationState(row["confirmation_state"]) == ConfirmationState.CONFIRMED  # 2 independent T1s

    result = _deliver(conn, config, row, now)

    assert result.action == "sent"
    assert result.is_statement_only is True

    rendered = render_alert(
        event_id="E-S3", category="US / China / World", english_headline=row["primary_title"],
        chinese_headline="特朗普关税声明", what_happened="Trump said tariffs would take effect.",
        why_it_matters="Could affect trade policy.", chinese_takeaway="尚未成为正式政策。",
        confirmation_state=ConfirmationState(row["confirmation_state"]), severity=Severity.S2,
        is_statement_only=True, sources=["wire-a.test", "wire-b.test"],
    )
    assert "Statement — policy not confirmed" in rendered
    assert "Confirmed" not in rendered.split("Status:")[1].split("\n")[0]


def test_scenario_4_quiet_hours_holds_s2_but_s3_is_immediate(config, conn):
    now_1am_pt = datetime(2026, 1, 15, 1, 0, tzinfo=PT)  # well outside DST, safely in quiet hours

    _seed_event(
        conn, event_id="E-S4-S2", title="BIS export control action on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_gov2", 0, "bis.gov")], now=now_1am_pt,
    )
    _seed_event(
        conn, event_id="E-S4-S3", title="Systemic financial instability triggers market shutdown",
        excerpt="Regulators reported systemic financial instability leading to a broad market shutdown.",
        sources=[("fed_gov2", 0, "federalreserve.gov")], now=now_1am_pt,
    )

    row_s2 = conn.execute("SELECT * FROM events WHERE id='E-S4-S2'").fetchone()
    row_s3 = conn.execute("SELECT * FROM events WHERE id='E-S4-S3'").fetchone()

    result_s2 = process_event_for_alerts(conn, config, row_s2, now_1am_pt)
    result_s3 = process_event_for_alerts(conn, config, row_s3, now_1am_pt)

    assert result_s2.action == "held_quiet_hours"
    assert result_s3.action == "sent"
    assert result_s3.severity == Severity.S3.value


def test_scenario_5_duplicate_articles_produce_one_alert(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    known = known_entities(config.topics.groups)

    item_a = CollectedItem(
        source_id="bis_gov3", external_id="a", canonical_url="https://bis.gov/a",
        title="BIS announces export control rule on advanced AI chips",
        summary_excerpt="BIS issued an export control rule on advanced AI chips.",
        language="en", published_at=now, origin_family="bis.gov",
    )
    item_b = CollectedItem(
        source_id="wire_dup", external_id="b", canonical_url="https://wire.test/b",
        title="BIS announces new export control rule for advanced AI chips",
        summary_excerpt="Wire report: BIS issued an export control rule for advanced AI chips.",
        language="en", published_at=now, origin_family="wire.test",
    )
    insert_source_row(conn, "bis_gov3", 0)
    insert_source_row(conn, "wire_dup", 1)

    from news_intel.pipeline.dedupe import upsert_raw_item

    for item in (item_a, item_b):
        normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)
        upsert = upsert_raw_item(conn, normalized)
        event_id, _ = find_or_create_event(conn, normalized, upsert.raw_item_id, config.policy.dedupe, known, now=now)

    events = conn.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]
    assert events == 1  # the two articles clustered into one event

    row = conn.execute("SELECT * FROM events").fetchone()
    conn.execute(
        "UPDATE events SET final_score=90, significance=85, market_impact=70 WHERE id=?", (row["id"],)
    )
    update_confirmation_state(conn, row["id"], now=now)
    row = conn.execute("SELECT * FROM events WHERE id=?", (row["id"],)).fetchone()

    result = _deliver(conn, config, row, now)
    assert result.action == "sent"

    count = conn.execute("SELECT COUNT(*) AS n FROM alert_queue").fetchone()["n"]
    assert count == 1


def test_scenario_6_cosmetic_followup_does_not_send_second_alert(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-S6", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_gov4", 0, "bis.gov")], now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-S6'").fetchone()
    first = _deliver(conn, config, row, now)
    assert first.action == "sent"

    # Cosmetic follow-up 1 hour later: no confirmation-state change occurred.
    later = now + timedelta(hours=1)
    row = conn.execute("SELECT * FROM events WHERE id='E-S6'").fetchone()
    second = process_event_for_alerts(conn, config, row, later)

    assert second.action == "suppressed_cooldown"
    sent_count = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-S6' AND state='sent'"
    ).fetchone()["n"]
    assert sent_count == 1


def test_scenario_7_formal_confirmation_after_statement_allows_material_update(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-S7", title="Trump said new tariffs on China would take effect",
        excerpt="Trump said in a statement that new tariffs on China would take effect next month.",
        sources=[("wire_a2", 1, "wire-a2.test"), ("wire_b2", 1, "wire-b2.test")],
        now=now,
    )
    row = conn.execute("SELECT * FROM events WHERE id='E-S7'").fetchone()
    first = _deliver(conn, config, row, now)
    assert first.action == "sent"
    assert first.is_statement_only is True

    # A formal policy document (T0) arrives an hour later -- a material update.
    later = now + timedelta(hours=1)
    insert_source_row(conn, "whitehouse_gov", 0)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family) VALUES "
        "('whitehouse_gov', 'doc1', 'https://whitehouse.gov/doc1', 'doc1', 'Formal tariff order signed', "
        "'formal tariff order signed', 'The White House published the formal tariff order.', 'en', ?, 'doc1', "
        "'whitehouse.gov')",
        (later.isoformat(),),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 0, '[]')",
        ("E-S7", raw_id),
    )
    update_confirmation_state(conn, "E-S7", now=later)
    row = conn.execute("SELECT * FROM events WHERE id='E-S7'").fetchone()
    assert ConfirmationState(row["confirmation_state"]) == ConfirmationState.OFFICIAL

    second = _deliver(conn, config, row, later)

    assert second.action == "sent"
    assert second.is_statement_only is False
    sent_count = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE event_id='E-S7' AND state='sent'"
    ).fetchone()["n"]
    assert sent_count == 2


def test_scenario_8_fourth_s2_in_one_day_deferred_to_digest(config, conn):
    base = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    actions = []
    for i in range(4):
        now = base + timedelta(minutes=40 * i)  # clears the 30-minute spacing rule
        event_id = f"E-S8-{i}"
        _seed_event(
            conn, event_id=event_id, title=f"BIS export control rule #{i}",
            excerpt="BIS announced another export control rule on chips.",
            sources=[(f"bis_gov_s8_{i}", 0, f"bis-{i}.gov")], now=now,
        )
        row = conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        actions.append(_deliver(conn, config, row, now))

    assert [a.action for a in actions[:3]] == ["sent", "sent", "sent"]
    assert actions[3].action == "suppressed_budget"

    sent_s2_today = conn.execute(
        "SELECT COUNT(*) AS n FROM alert_queue WHERE state='sent' AND severity='S2'"
    ).fetchone()["n"]
    assert sent_s2_today == 3
