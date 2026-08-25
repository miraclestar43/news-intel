"""2026-08-25 repair: real production defect reproduced against a copy of
the production database (a Ukraine mall-strike event) -- events.
primary_title was frozen at event-creation time (pipeline/cluster.py's
find_or_create_event never updated it on merge), so when a fuller,
better-attributed article about the SAME event merged in later, the
digest kept showing the stale, less-attributed snapshot ("...condemns
'despicable' strike") instead of the merged source's own title
("...condemns 'despicable' Russian strike") -- dropping actor attribution
from the reader-facing headline. General fix (length-based "more
complete" proxy, not a Russian/Ukraine-specific patch), in two places:
pipeline/cluster.py's merge branch now updates primary_title to the
longer of the current and incoming titles; event_repo._display_title adds
the same comparison against sources[0]'s own raw title as a safety net
for data merged before this fix existed."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.adapters.base import CollectedItem
from news_intel.event_repo import event_as_digest_dict
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.dedupe import upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from tests.conftest import insert_source_row


def _store(conn, config, *, source_id, title, excerpt, url, now):
    insert_source_row(conn, source_id, 0)
    item = CollectedItem(
        source_id=source_id, external_id=url, canonical_url=url, title=title,
        summary_excerpt=excerpt, language="en", published_at=now, origin_family=source_id, metadata={},
    )
    normalized = normalize(item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)
    upsert = upsert_raw_item(conn, normalized)
    known = known_entities(config.topics.groups)
    event_id, is_new = find_or_create_event(
        conn, normalized, upsert.raw_item_id, config.policy.dedupe, known,
        now=now, time_policy=config.policy.time_semantics,
        legal_notice_policy=config.topics.legal_notice_boilerplate,
    )
    return event_id, is_new


def test_merged_source_attacker_attribution_survives_to_display_title(config, conn):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    wire_title = "Rescuers dig through Ukraine mall wreckage as Zelensky condemns 'despicable' strike"
    bbc_title = "Rescuers dig through Ukraine mall wreckage as Zelensky condemns 'despicable' Russian strike"

    event_id, is_new = _store(
        conn, config, source_id="wire_feed", title=wire_title,
        excerpt="Rescuers dug through wreckage after a strike on a shopping mall.",
        url="https://wire.example/mall-strike", now=now,
    )
    assert is_new

    event_id_2, is_new_2 = _store(
        conn, config, source_id="bbc_feed", title=bbc_title,
        excerpt="Rescuers dug through wreckage after a Russian strike hit a shopping mall in Ukraine, "
                "Zelensky said.",
        url="https://bbc.example/mall-strike", now=now,
    )
    assert not is_new_2
    assert event_id_2 == event_id

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    # The merge branch itself now keeps primary_title current -- this pins
    # the root-cause fix, not just the display-time safety net.
    assert "Russian" in row["primary_title"]

    digest = event_as_digest_dict(conn, row)
    assert "Russian" in digest["title"], (
        f"attacker attribution dropped from displayed title: {digest['title']!r}"
    )


def test_display_title_safety_net_when_primary_title_column_is_stale(config, conn):
    """Reproduces the exact real-production row shape found in a copy of
    the production database for the Ukraine mall-strike event: only ONE
    event_sources row survives (the production retention/pruning path
    that removed the original creator's row is out of scope here -- what
    matters is the resulting shape), it is flagged is_primary=0, and the
    events.primary_title column itself was never updated (legacy pre-fix
    data). _display_title must still surface the surviving source's own
    (more complete) raw title rather than the stale column."""
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    short_title = "Rescuers dig through Ukraine mall wreckage as Zelensky condemns 'despicable' strike"
    long_title = "Rescuers dig through Ukraine mall wreckage as Zelensky condemns 'despicable' Russian strike"

    event_id, is_new = _store(
        conn, config, source_id="wire_feed2", title=short_title,
        excerpt="Rescuers dug through wreckage after a strike on a shopping mall.",
        url="https://wire2.example/mall-strike", now=now,
    )
    assert is_new

    from news_intel.adapters.base import CollectedItem
    from news_intel.pipeline.cluster import _attach_source
    from news_intel.pipeline.dedupe import upsert_raw_item
    from news_intel.pipeline.normalize import normalize

    insert_source_row(conn, "bbc_feed2", 0)
    bbc_item = CollectedItem(
        source_id="bbc_feed2", external_id="https://bbc2.example/mall-strike",
        canonical_url="https://bbc2.example/mall-strike", title=long_title,
        summary_excerpt="Rescuers dug through wreckage after a Russian strike hit a shopping mall.",
        language="en", published_at=now, origin_family="bbc_feed2", metadata={},
    )
    normalized = normalize(bbc_item, excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars, now=now)
    upsert = upsert_raw_item(conn, normalized)
    _attach_source(conn, event_id, upsert.raw_item_id, is_primary=False)
    # Force primary_title back to the stale value (this test targets the
    # legacy-data safety net specifically, independent of whether the
    # merge-time fix also ran) and drop the original creator's own
    # event_sources row, matching the real production row shape found.
    conn.execute("UPDATE events SET primary_title = ? WHERE id = ?", (short_title, event_id))
    conn.execute(
        "DELETE FROM event_sources WHERE event_id = ? AND raw_item_id != ?", (event_id, upsert.raw_item_id)
    )

    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    assert row["primary_title"] == short_title  # stale, as legacy data would be

    digest = event_as_digest_dict(conn, row)
    assert "Russian" in digest["title"], (
        f"safety net failed to surface attribution from the surviving source: {digest['title']!r}"
    )


def test_display_title_unchanged_when_no_merge_occurred(config, conn):
    """Backward compatibility: the common single-source case must render
    identically to before this fix -- sources[0]'s raw title equals the
    frozen primary_title snapshot when nothing ever merged."""
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    title = "A single-source story with no merge"
    event_id, is_new = _store(
        conn, config, source_id="solo_feed", title=title,
        excerpt="A complete sentence describing the event happened here.",
        url="https://solo.example/story", now=now,
    )
    assert is_new
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    digest = event_as_digest_dict(conn, row)
    assert digest["title"] == title
