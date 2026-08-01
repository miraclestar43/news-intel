from datetime import UTC, datetime

from news_intel.models import ConfirmationState
from news_intel.pipeline.confirmation import update_confirmation_state
from tests.conftest import insert_source_row


def _make_event(conn, event_id="E-20260731-0001"):
    now = datetime.now(UTC).isoformat()
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at) VALUES (?, 'test', ?, ?)",
        (event_id, now, now),
    )
    return event_id


def _attach_raw_item(conn, event_id, source_id, tier, origin_family, ext_id):
    insert_source_row(conn, source_id, tier)
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, "
        "normalized_title, language, fetched_at, content_hash, origin_family) "
        "VALUES (?, ?, ?, ?, 'title', 'title', 'en', ?, ?, ?)",
        (source_id, ext_id, f"https://x.test/{ext_id}", ext_id, datetime.now(UTC).isoformat(), ext_id, origin_family),
    )
    raw_id = cur.lastrowid
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 0, '[]')",
        (event_id, raw_id),
    )


def test_single_t3_source_is_unverified(conn):
    event_id = _make_event(conn)
    _attach_raw_item(conn, event_id, "hn_top", 3, "news.ycombinator.com", "a1")
    state = update_confirmation_state(conn, event_id)
    assert state == ConfirmationState.UNVERIFIED


def test_two_independent_t1_sources_confirm(conn):
    event_id = _make_event(conn)
    _attach_raw_item(conn, event_id, "wire_one", 1, "wire-one.test", "a1")
    _attach_raw_item(conn, event_id, "wire_two", 1, "wire-two.test", "a2")
    state = update_confirmation_state(conn, event_id)
    assert state == ConfirmationState.CONFIRMED


def test_two_t1_sources_same_origin_family_do_not_confirm(conn):
    event_id = _make_event(conn)
    _attach_raw_item(conn, event_id, "wire_one", 1, "shared-wire.test", "a1")
    _attach_raw_item(conn, event_id, "wire_two", 1, "shared-wire.test", "a2")
    state = update_confirmation_state(conn, event_id)
    assert state == ConfirmationState.REPORTED


def test_single_t0_source_is_official(conn):
    event_id = _make_event(conn)
    _attach_raw_item(conn, event_id, "federal_register", 0, "federalregister.gov", "a1")
    state = update_confirmation_state(conn, event_id)
    assert state == ConfirmationState.OFFICIAL


def test_disputed_state_is_sticky(conn):
    event_id = _make_event(conn)
    conn.execute("UPDATE events SET confirmation_state = 'disputed' WHERE id = ?", (event_id,))
    _attach_raw_item(conn, event_id, "federal_register", 0, "federalregister.gov", "a1")
    state = update_confirmation_state(conn, event_id)
    assert state == ConfirmationState.DISPUTED
