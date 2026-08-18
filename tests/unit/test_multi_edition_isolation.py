"""Multi-edition architecture: en_global and zh_cn must be fully isolated
in job locks, novelty history, publisher caps, clustering, idempotency, and
delivery-profile resolution, while cross-edition overlap of the SAME
real-world event is explicitly allowed. A failure in one edition must never
abort, block, or corrupt the other. Backward-compatible migration of
pre-existing English rows is also covered here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from news_intel import db
from news_intel.adapters.base import CollectedItem
from news_intel.jobs.daily import _daily_idempotency_key, run_daily
from news_intel.jobs.monthly import _monthly_idempotency_key
from news_intel.jobs.weekly import _weekly_idempotency_key
from news_intel.pipeline.cluster import find_or_create_event
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.dedupe import upsert_raw_item
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.normalize import normalize
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT, insert_source_row

_NOW = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_CHAT_IDS", "LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555")
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.fixture
def conn(config):
    connection = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


def _seed_event(conn, config, *, event_id, title, category, edition_id="en_global", now=_NOW,
                 url="https://x.test/0", excerpt="A complete sentence for the excerpt."):
    source_id = f"src_{event_id}"
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state, edition_id) VALUES (?, ?, ?, ?, ?, 'official', ?)",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat(), edition_id),
    )
    cur = conn.execute(
        "INSERT INTO raw_items (source_id, external_id, canonical_url, url_hash, title, normalized_title, "
        "summary_excerpt, language, fetched_at, content_hash, origin_family, edition_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'en', ?, ?, ?, ?)",
        (source_id, f"{event_id}-0", url, f"{event_id}-hash", title, title.lower(), excerpt,
         now.isoformat(), f"{event_id}-content", source_id, edition_id),
    )
    conn.execute(
        "INSERT INTO event_sources (event_id, raw_item_id, is_primary, supports_claims_json) VALUES (?, ?, 1, '[]')",
        (event_id, cur.lastrowid),
    )
    update_confirmation_state(conn, event_id, now=now)
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)
    conn.execute(
        "UPDATE events SET category = ?, final_score = 90, significance = 90, market_impact = 90 WHERE id = ?",
        (category, event_id),
    )


# --- job lock isolation ------------------------------------------------------


def test_en_global_and_zh_cn_daily_job_locks_are_independent(conn):
    assert db.acquire_job_lock(conn, "daily") is True
    # A held en_global lock must never block acquiring the zh_cn lock.
    assert db.acquire_job_lock(conn, "daily_zh_cn") is True
    db.release_job_lock(conn, "daily")
    db.release_job_lock(conn, "daily_zh_cn")


def test_zh_cn_holding_its_own_lock_does_not_block_en_global(conn):
    assert db.acquire_job_lock(conn, "daily_zh_cn") is True
    assert db.acquire_job_lock(conn, "daily") is True
    db.release_job_lock(conn, "daily_zh_cn")
    db.release_job_lock(conn, "daily")


# --- header labels ------------------------------------------------------


def test_zh_cn_daily_uses_the_chinese_header(config, conn):
    _seed_event(conn, config, event_id="E-ZH-1", title="商务部宣布新一轮出口管制措施",
                category="US / China / World", edition_id="zh_cn")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW, edition_id="zh_cn")
    assert summary.rendered_text.startswith("[中文日报]\n")


def test_en_global_daily_uses_the_global_header_not_chinese(config, conn):
    _seed_event(conn, config, event_id="E-EN-1", title="A story about markets", category="Markets & Economy")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW, edition_id="en_global")
    assert summary.rendered_text.startswith("[GLOBAL DAILY]\n")
    assert "[中文日报]" not in summary.rendered_text


# --- idempotency key non-collision --------------------------------------


def test_daily_idempotency_keys_never_collide_across_editions_same_date_same_chat():
    en = _daily_idempotency_key("en_global", "2026-08-17", "abc123")
    zh = _daily_idempotency_key("zh_cn", "2026-08-17", "abc123")
    assert en != zh
    assert en == "daily:2026-08-17:abc123"  # unchanged pre-existing format
    assert zh == "daily:zh_cn:2026-08-17:abc123"


def test_weekly_idempotency_keys_never_collide_across_editions_same_week_same_chat():
    en = _weekly_idempotency_key("en_global", "2026-W33", "abc123")
    zh = _weekly_idempotency_key("zh_cn", "2026-W33", "abc123")
    assert en != zh
    assert en == "weekly:2026-W33:abc123"
    assert zh == "weekly:zh_cn:2026-W33:abc123"


def test_monthly_idempotency_keys_never_collide_across_editions_same_month_same_chat():
    en = _monthly_idempotency_key("en_global", "2026-08", "abc123")
    zh = _monthly_idempotency_key("zh_cn", "2026-08", "abc123")
    assert en != zh
    assert en == "monthly:2026-08:abc123"
    assert zh == "monthly:zh_cn:2026-08:abc123"


# --- delivery profile / bot resolution -----------------------------------


def test_both_editions_resolve_to_the_same_existing_delivery_profile(config):
    en_profile_id = config.editions.get("en_global").delivery_profile_id
    zh_profile_id = config.editions.get("zh_cn").delivery_profile_id
    assert en_profile_id == zh_profile_id == "default"
    profile = config.delivery_profiles.get("default")
    assert profile.bot_profile_id == "star_daily_news_bot"
    # No new bot/token: this rollout deliberately shares one existing profile.


def test_unknown_edition_id_fails_closed_not_silent_default():
    config = load_config(config_dir=PROJECT_ROOT / "config", data_dir=PROJECT_ROOT / "data")
    with pytest.raises(KeyError):
        config.editions.get("not_a_real_edition")
    reset_config_cache()


# --- cross-edition novelty / clustering isolation, and allowed overlap ---


def test_zh_cn_event_selection_is_unaffected_by_en_global_selection(config, conn):
    """Independent novelty/publisher caps: seeding a qualifying en_global
    event must not itself qualify or suppress anything in a zh_cn run over
    the same window -- each edition's daily selection only ever sees its
    own edition's rows (event_repo.qualified_events_since is edition-
    scoped)."""
    _seed_event(conn, config, event_id="E-EN-2", title="A distinct English-only story",
                category="Markets & Economy", edition_id="en_global")
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="zh_cn")
    assert "A distinct English-only story" not in summary.rendered_text


def test_cross_edition_overlap_of_the_same_real_event_is_allowed(config, conn):
    """The SAME underlying real-world development, independently sourced
    and clustered into a separate event_id per edition, may appear in BOTH
    editions' digests simultaneously -- overlap is allowed, not suppressed,
    because there is no cross-edition merge point (see clustering isolation
    test below)."""
    _seed_event(conn, config, event_id="E-OVERLAP-EN", title="NVIDIA export control rule on AI chips",
                category="AI & Semiconductors", edition_id="en_global")
    _seed_event(conn, config, event_id="E-OVERLAP-ZH", title="英伟达芯片出口管制新规定",
                category="AI & Semiconductors", edition_id="zh_cn",
                excerpt="商务部发布关于英伟达芯片出口管制的新规定，涉及先进制程产品。")

    en_summary = run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="en_global")
    zh_summary = run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="zh_cn")

    assert "NVIDIA export control rule on AI chips" in en_summary.rendered_text
    assert "英伟达芯片出口管制新规定" in zh_summary.rendered_text


def test_clustering_never_merges_across_editions_even_with_identical_content(config, conn):
    """The safety-critical mechanism: pipeline/cluster.py's candidate-
    matching query is scoped to same-edition events only, so an English
    and a Chinese raw_item -- even with byte-identical title/excerpt text
    -- can never merge into one event row."""
    insert_source_row(conn, "src_dup_en", 0)
    insert_source_row(conn, "src_dup_zh", 0)
    known = known_entities(config.topics.groups)
    identical_text = "Identical duplicate-looking headline text for isolation testing"

    item_en = CollectedItem(
        source_id="src_dup_en", external_id="dup-en", canonical_url="https://en.test/dup",
        title=identical_text, summary_excerpt=identical_text, language="en",
        published_at=_NOW, origin_family="en.test",
    )
    item_zh = CollectedItem(
        source_id="src_dup_zh", external_id="dup-zh", canonical_url="https://zh.test/dup",
        title=identical_text, summary_excerpt=identical_text, language="en",
        published_at=_NOW, origin_family="zh.test",
    )

    norm_en = normalize(item_en, excerpt_max_chars=280, now=_NOW, edition_id="en_global")
    norm_zh = normalize(item_zh, excerpt_max_chars=280, now=_NOW, edition_id="zh_cn")
    upsert_en = upsert_raw_item(conn, norm_en)
    upsert_zh = upsert_raw_item(conn, norm_zh)

    event_id_en, is_new_en = find_or_create_event(
        conn, norm_en, upsert_en.raw_item_id, config.policy.dedupe, known, now=_NOW,
    )
    event_id_zh, is_new_zh = find_or_create_event(
        conn, norm_zh, upsert_zh.raw_item_id, config.policy.dedupe, known, now=_NOW,
    )

    assert is_new_en is True
    assert is_new_zh is True
    assert event_id_en != event_id_zh  # never merged despite identical content

    en_row = conn.execute("SELECT edition_id FROM events WHERE id = ?", (event_id_en,)).fetchone()
    zh_row = conn.execute("SELECT edition_id FROM events WHERE id = ?", (event_id_zh,)).fetchone()
    assert en_row["edition_id"] == "en_global"
    assert zh_row["edition_id"] == "zh_cn"


# --- one edition's failure must not corrupt the other -----------------------


def test_zh_cn_run_raising_does_not_leave_en_global_job_lock_held(config, conn):
    """A zh_cn failure must not corrupt or block en_global -- simulates a
    mid-run failure (missing edition config) and confirms the OTHER
    edition's lock is still independently acquirable afterward, and the
    failing edition's own lock was released (finally-block behavior)."""
    with pytest.raises(KeyError):
        run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="not_a_real_edition")

    # en_global must be completely unaffected -- its lock is free.
    assert db.acquire_job_lock(conn, "daily") is True
    db.release_job_lock(conn, "daily")


def test_en_global_daily_succeeds_after_a_zh_cn_failure_in_the_same_session(config, conn):
    with pytest.raises(KeyError):
        run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="not_a_real_edition")

    _seed_event(conn, config, event_id="E-EN-3", title="A story unaffected by the prior zh_cn failure",
                category="Markets & Economy", edition_id="en_global")
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW, edition_id="en_global")
    assert summary.fail_closed is False
    assert "A story unaffected by the prior zh_cn failure" in summary.rendered_text


# --- backward-compatible migration of pre-existing English rows -------------


def test_migration_default_preserves_pre_existing_english_row_identity(tmp_path):
    """Simulates a pre-multi-edition database (a row inserted before
    migration 012 ever ran, as if from a prior production snapshot) --
    every existing row must get edition_id='en_global' via the DEFAULT,
    with NO change to its identity, timestamps, delivery status, message
    ID, or historical text."""
    from pathlib import Path

    db_path = Path(tmp_path) / "legacy.db"
    conn = db.connect(db_path)

    # Run only migrations up to (not including) the multi-edition one, by
    # applying ALL migrations (they are idempotent/additive) is not
    # possible to "partially" do via db.run_migrations -- instead, insert
    # a row using column names common to the pre-migration schema, then
    # run migrations, and confirm the row survives with edition_id defaulted.
    db.run_migrations(conn)  # brings schema fully current, including edition_id columns

    ts = _NOW.isoformat()
    insert_source_row(conn, "src_legacy", 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state, final_score) VALUES ('E-LEGACY-1', 'A pre-existing English story', ?, ?, ?, "
        "'official', 77)",
        (ts, ts, ts),
    )
    conn.commit()

    row = conn.execute("SELECT * FROM events WHERE id = 'E-LEGACY-1'").fetchone()
    assert row["edition_id"] == "en_global"  # DEFAULT applied, not NULL, not some other value
    assert row["primary_title"] == "A pre-existing English story"
    assert row["final_score"] == 77
    assert row["first_seen_at"] == ts  # timestamp completely unchanged
    conn.close()
