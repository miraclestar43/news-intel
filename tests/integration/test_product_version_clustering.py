"""Gate A remediation Retry 1 Blocker 2: reproduces the exact reported
same-event over-cluster ("Introducing GPT-5.5" / "GPT-5.4" / "GPT-5.2" /
"GPT-5" / "GPTs" merging into one event) as a regression fixture and
proves the fix."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.adapters.base import CollectedItem
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


def test_gate_a_retry1_openai_version_posts_no_longer_merge(config, conn):
    """Regression fixture reproducing the exact Retry 1 finding: five
    distinct OpenAI model-launch announcements must become five distinct
    events, not one."""
    now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    titles = [
        ("Introducing GPT-5.5", "https://openai.com/index/introducing-gpt-5-5/"),
        ("Introducing GPT-5.4", "https://openai.com/index/introducing-gpt-5-4/"),
        ("Introducing GPT-5.2", "https://openai.com/index/introducing-gpt-5-2/"),
        ("Introducing GPT-5", "https://openai.com/index/introducing-gpt-5/"),
        ("Introducing GPTs", "https://openai.com/index/introducing-gpts/"),
    ]
    event_ids = []
    for title, url in titles:
        eid, is_new = _store(
            conn, config, source_id="openai_news", title=title,
            excerpt=f"OpenAI today announced {title}.", url=url, now=now,
        )
        event_ids.append(eid)

    assert len(set(event_ids)) == 5, f"expected 5 distinct events, got {event_ids}"


def test_high_title_similarity_cannot_override_conflicting_version(config, conn):
    now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    a_id, _ = _store(
        conn, config, source_id="openai_news", title="Introducing GPT-5.5",
        excerpt="OpenAI today announced GPT-5.5.", url="https://openai.com/a", now=now,
    )
    b_id, is_new_b = _store(
        conn, config, source_id="openai_news", title="Introducing GPT-5.4",
        excerpt="OpenAI today announced GPT-5.4.", url="https://openai.com/b", now=now,
    )
    # These titles differ by a single character -- title_similarity_score
    # would clear any reasonable threshold -- yet must not merge.
    assert is_new_b is True
    assert a_id != b_id


def test_syndicated_coverage_of_the_same_version_still_clusters(config, conn):
    """A wire outlet re-syndicating the same announcement (same version,
    near-identical title -- what actually clears the pre-existing
    title-similarity bar) must still cluster as one event."""
    now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    a_id, _ = _store(
        conn, config, source_id="openai_news", title="Introducing GPT-5.5",
        excerpt="OpenAI today announced GPT-5.5, its newest model.", url="https://openai.com/a", now=now,
    )
    b_id, is_new_b = _store(
        conn, config, source_id="techmeme", title="Introducing GPT-5.5",
        excerpt="Wire coverage: OpenAI today announced GPT-5.5, its newest model.",
        url="https://techmeme.com/b", now=now,
    )
    assert is_new_b is False
    assert a_id == b_id


def test_gpts_plural_does_not_conflict_with_a_numbered_gpt_release(config, conn):
    """"GPTs" (the custom-GPTs feature) carries no version claim, so it is
    compatible-by-absence with a numbered release -- it just won't merge
    with it either unless title/trigram similarity independently clears
    the same-event bar, which two genuinely different announcements won't."""
    now = datetime(2026, 8, 4, 15, 0, tzinfo=UTC)
    a_id, _ = _store(
        conn, config, source_id="openai_news", title="Introducing GPT-5",
        excerpt="OpenAI today announced GPT-5.", url="https://openai.com/gpt5", now=now,
    )
    b_id, is_new_b = _store(
        conn, config, source_id="openai_news", title="Introducing GPTs",
        excerpt="OpenAI today announced GPTs, a way to customize ChatGPT.", url="https://openai.com/gpts", now=now,
    )
    assert is_new_b is True  # distinct announcements, correctly separate
    assert a_id != b_id
