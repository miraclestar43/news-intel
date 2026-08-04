"""Gate A remediation Phase 3.4: templated law-firm shareholder-alert
boilerplate must never cluster two different tickers/defendants into one
event merely because their titles/trigrams are near-identical."""

from __future__ import annotations

from datetime import UTC, datetime

from news_intel.adapters.base import CollectedItem
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.dedupe import upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from tests.conftest import insert_source_row


def _store(conn, config, *, source_id, title, excerpt, url, now):
    insert_source_row(conn, source_id, 2)
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


def test_different_ticker_legal_notices_do_not_cluster_despite_boilerplate_similarity(config, conn):
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    first_id, _ = _store(
        conn, config, source_id="law_firm_wire",
        title="ROSEN, A TOP RANKED LAW FIRM, Encourages Alpha Corp Investors to Secure Counsel",
        excerpt="If you purchased shares of Alpha Corp between January and March, contact our firm "
        "before the important deadline in this securities fraud class action.",
        url="https://lawfirm.test/alpha-corp-notice", now=now,
    )
    second_id, is_new = _store(
        conn, config, source_id="law_firm_wire",
        title="ROSEN, A TOP RANKED LAW FIRM, Encourages Beta Industries Investors to Secure Counsel",
        excerpt="If you purchased shares of Beta Industries between April and June, contact our firm "
        "before the important deadline in this securities fraud class action.",
        url="https://lawfirm.test/beta-industries-notice", now=now,
    )

    assert is_new is True  # a second, distinct defendant must get its own event
    assert first_id != second_id

    sources_first = conn.execute(
        "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (first_id,)
    ).fetchone()["n"]
    sources_second = conn.execute(
        "SELECT COUNT(*) AS n FROM event_sources WHERE event_id = ?", (second_id,)
    ).fetchone()["n"]
    assert sources_first == 1
    assert sources_second == 1


def test_same_boilerplate_notice_from_two_wires_still_clusters_when_a_known_entity_matches(config, conn):
    """The guard must not become "boilerplate items can never cluster at
    all" -- if the same known company is genuinely named in both, they
    should still merge as corroborating coverage of the same notice."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    first_id, _ = _store(
        conn, config, source_id="wire_a",
        title="ROSEN, A TOP RANKED LAW FIRM, Encourages NVIDIA Investors to Secure Counsel",
        excerpt="If you purchased shares of NVIDIA between January and March, contact our firm "
        "before the important deadline in this securities fraud class action.",
        url="https://lawfirm.test/nvidia-notice-a", now=now,
    )
    second_id, is_new = _store(
        conn, config, source_id="wire_b",
        title="Shareholder rights firm encourages NVIDIA investors to secure counsel",
        excerpt="If you purchased shares of NVIDIA between January and March, our shareholder "
        "rights firm reminds investors of the important deadline in this class action lawsuit.",
        url="https://lawfirm.test/nvidia-notice-b", now=now,
    )

    assert first_id == second_id
    assert is_new is False


def test_legal_notice_with_material_override_clusters_normally_by_title_similarity(config, conn):
    """Once a material corporate event independently qualifies (here via a
    material_override_phrase), the item is no longer flagged as boilerplate
    and ordinary title-similarity clustering applies."""
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    first_id, _ = _store(
        conn, config, source_id="wire_a",
        title="Example Corp discloses material weakness and restates financial results",
        excerpt="Example Corp disclosed a material weakness in internal controls and restated "
        "financial results for the prior two fiscal years, prompting a law firm investigation.",
        url="https://wire-a.test/example-corp-restatement", now=now,
    )
    second_id, is_new = _store(
        conn, config, source_id="wire_b",
        title="Example Corp discloses material weakness, restates financial results",
        excerpt="Example Corp disclosed a material weakness in its internal controls and has "
        "restated financial results for the prior two fiscal years, a law firm said.",
        url="https://wire-b.test/example-corp-restatement-2", now=now,
    )

    assert first_id == second_id
    assert is_new is False
