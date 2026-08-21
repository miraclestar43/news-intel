"""2026-08-21 repair: scheduled_events (BLS/BEA/Fed calendar releases) had
no edition_id at all, and upcoming() had no edition filter -- the identical
en_global-only US economic-calendar item was rendered verbatim as zh_cn's
"明日关注" too. Regression coverage for the edition scoping fix."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from news_intel.adapters.base import CollectedItem
from news_intel.scheduled_repo import record_scheduled_events, upcoming
from tests.conftest import insert_source_row


def test_calendar_items_are_scoped_to_their_own_edition(conn):
    now = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    insert_source_row(conn, "bls_calendar", 0, adapter_type="ical")
    record_scheduled_events(
        conn,
        "bls_calendar",
        [
            CollectedItem(
                source_id="bls_calendar",
                external_id="cpi-aug",
                canonical_url="https://www.bls.gov/schedule/news_release/cpi.htm",
                title="CPI release — August",
                summary_excerpt=None,
                language="en",
                published_at=now + timedelta(hours=20),
                origin_family="scheduled_calendar",
            )
        ],
        now=now,
        edition_id="en_global",
    )

    en_items = upcoming(conn, within_hours=60, now=now, edition_id="en_global")
    zh_items = upcoming(conn, within_hours=60, now=now, edition_id="zh_cn")

    assert len(en_items) == 1
    assert en_items[0]["title"] == "CPI release — August"
    assert len(zh_items) == 0
