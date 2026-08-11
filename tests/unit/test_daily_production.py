"""Production promotion of pi-news daily delivery (2026-08-09): the three
observed presentation corrections (dynamic TOP heading, no mid-sentence
fallback summaries, replaced banner text), 4096-char enforcement,
production-date idempotency, no-retry-after-failed/ambiguous-send, and the
CLI's pre-start-date safety-net skip."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from typer.testing import CliRunner

from news_intel import db
from news_intel.delivery.formatter import render_daily
from news_intel.jobs.daily import run_daily
from news_intel.llm.fallback import FALLBACK_NOTICE, _complete_sentence_summary, fallback_daily_digest
from news_intel.models import DailyDigestInput
from news_intel.pipeline.confirmation import update_confirmation_state
from news_intel.pipeline.scoring import persist_score, score_event
from news_intel.settings import load_config, reset_config_cache
from tests.conftest import PROJECT_ROOT, insert_source_row

_NOW = datetime(2026, 8, 10, 22, 0, tzinfo=UTC)  # 2026-08-10 15:00 PDT, a Monday (not the weekly-brief Sunday)


@pytest.fixture
def config(tmp_path, monkeypatch):
    """Overrides the shared `config` fixture: most tests in this file pass
    chat_id=555 to exercise production fail-closed/send behavior, so a
    single allowlisted chat is configured by default here."""
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


def _candidate(title="A story", excerpt="Something happened.", category="AI & Semiconductors"):
    return {"title": title, "category": category, "excerpt": excerpt, "sources": [{"publisher": "Test Pub"}]}


# --- presentation fix 1: dynamic heading -----------------------------------


def test_fallback_heading_top_story_for_one_item():
    inputs = DailyDigestInput(date_label="Aug 9", top3_candidates=[_candidate()], quick_brief_candidates=[],
                               calendar_candidates=[])
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "TOP STORY" in out.rendered_text
    assert "TOP 2" not in out.rendered_text
    assert "TOP 3" not in out.rendered_text


def test_fallback_heading_top_2_for_two_items():
    inputs = DailyDigestInput(
        date_label="Aug 9", top3_candidates=[_candidate("A"), _candidate("B")],
        quick_brief_candidates=[], calendar_candidates=[],
    )
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "TOP 2" in out.rendered_text
    assert "TOP STORY" not in out.rendered_text
    assert "TOP 3" not in out.rendered_text


def test_fallback_heading_top_3_for_three_items():
    inputs = DailyDigestInput(
        date_label="Aug 9", top3_candidates=[_candidate("A"), _candidate("B"), _candidate("C")],
        quick_brief_candidates=[], calendar_candidates=[],
    )
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "TOP 3" in out.rendered_text


def test_fallback_no_heading_when_no_qualifying_events():
    inputs = DailyDigestInput(date_label="Aug 9", top3_candidates=[], quick_brief_candidates=[], calendar_candidates=[])
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "TOP" not in out.rendered_text


def test_render_daily_heading_matches_llm_path_too():
    structured = {"top3": [{"english_headline": "H", "chinese_headline": "H"}], "quick_briefs": []}
    rendered = render_daily("Aug 9", structured)
    assert "TOP STORY" in rendered


# --- presentation fix 2: no mid-sentence summaries --------------------------


def test_complete_sentence_summary_keeps_full_sentences_within_budget():
    excerpt = "First sentence is here. Second sentence follows on too."
    result = _complete_sentence_summary(excerpt, max_chars=280)
    assert result == "First sentence is here. Second sentence follows on too."


def test_complete_sentence_summary_drops_trailing_partial_sentence():
    excerpt = "First sentence is complete. " + ("word " * 100)  # second sentence never terminates in budget
    result = _complete_sentence_summary(excerpt, max_chars=60)
    assert result == "First sentence is complete."
    assert not result.endswith("word")


def test_complete_sentence_summary_returns_none_when_no_sentence_completes():
    excerpt = "a run-on fragment with no terminal punctuation at all " * 5
    result = _complete_sentence_summary(excerpt, max_chars=40)
    assert result is None


def test_complete_sentence_summary_returns_none_for_empty_excerpt():
    assert _complete_sentence_summary("") is None
    assert _complete_sentence_summary(None) is None


def test_fallback_digest_omits_summary_line_when_no_complete_sentence_exists():
    """Reproduces the canary 3/3 defect: a real excerpt with no sentence-
    ending punctuation within the 280-char budget must never appear
    truncated mid-word/mid-sentence -- it is omitted, title/category/source
    are kept."""
    long_run_on = (
        "The global buildout of AI infrastructure reached a new milestone today — Firebird an "
        "emerging AI cloud launched the CIS region's largest AI factory in Armenia establishing a "
        "new AI computing hub powered by NVIDIA accelerated computing and Dell Technologies high "
        "performance AI systems with no period anywhere in this entire run-on fragment at all"
    )
    inputs = DailyDigestInput(
        date_label="Aug 9",
        top3_candidates=[_candidate("Firebird Launches AI Factory", excerpt=long_run_on)],
        quick_brief_candidates=[], calendar_candidates=[],
    )
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "Firebird Launches AI Factory" in out.rendered_text
    assert "Category: AI & Semiconductors" in out.rendered_text
    assert "Sources: Test Pub" in out.rendered_text
    assert "no period anywhere" not in out.rendered_text  # the mid-sentence fragment never appears
    assert not out.rendered_text.rstrip().endswith("AI")  # would be the old truncated ending


# --- presentation fix 3: banner text ----------------------------------------


def test_fallback_notice_is_the_new_text_not_the_old_one():
    assert FALLBACK_NOTICE == "This brief uses rule-ranked source summaries."
    assert "AI summarization is temporarily unavailable" not in FALLBACK_NOTICE


def test_fallback_digest_contains_new_banner_text():
    inputs = DailyDigestInput(date_label="Aug 9", top3_candidates=[_candidate()], quick_brief_candidates=[],
                               calendar_candidates=[])
    out = fallback_daily_digest(inputs, "Aug 9")
    assert "This brief uses rule-ranked source summaries." in out.rendered_text
    assert "AI summarization is temporarily unavailable" not in out.rendered_text


# --- production hardening: seeding helper -----------------------------------


def _seed_event(conn, config, *, event_id, title, category, now=_NOW, url="https://x.test/0",
                 excerpt="A complete sentence for the excerpt."):
    source_id = f"src_{event_id}"
    insert_source_row(conn, source_id, 0)
    conn.execute(
        "INSERT INTO events (id, primary_title, first_seen_at, last_updated_at, published_anchor_at, "
        "confirmation_state) VALUES (?, ?, ?, ?, ?, 'official')",
        (event_id, title, now.isoformat(), now.isoformat(), now.isoformat()),
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
    update_confirmation_state(conn, event_id, now=now)
    scores = score_event(conn, event_id, config, is_new_event=True, now=now)
    weights = config.policy.scoring.weights.model_dump()
    persist_score(conn, event_id, scores, weights, config.policy.scoring.thresholds)
    # score_event's own classify_relevance overwrites category based on
    # text content -- set the test's intended category last, after scoring.
    conn.execute(
        "UPDATE events SET category = ?, final_score = 90, significance = 90, market_impact = 90 WHERE id = ?",
        (category, event_id),
    )


# --- 4096-char enforcement ---------------------------------------------------


def test_run_daily_fails_closed_when_rendered_exceeds_hard_limit(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A" * 30, category="AI & Semiconductors")
    config.policy.digests.telegram_hard_limit_chars = 10  # trivially exceeded by any real digest

    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)

    assert summary.fail_closed is True
    assert "exceeds" in summary.fail_reason
    assert conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"] == 0


# --- production-date idempotency / no retry ---------------------------------


def test_run_daily_second_real_send_for_same_pacific_date_is_not_retried(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A qualifying story", category="AI & Semiconductors")

    first = run_daily(config, conn, chat_id=555, dry_run=False, now=_NOW)
    assert first.fail_closed is False

    second = run_daily(config, conn, chat_id=555, dry_run=False, now=_NOW)
    assert second.fail_closed is True
    assert "already been attempted" in second.fail_reason

    count = conn.execute(
        "SELECT COUNT(*) AS n FROM deliveries WHERE idempotency_key = ?", (first.idempotency_key,)
    ).fetchone()["n"]
    assert count == 1


def test_run_daily_failed_send_pre_mark_blocks_retry(config, conn, monkeypatch):
    _seed_event(conn, config, event_id="E-1", title="A qualifying story", category="AI & Semiconductors")

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated network failure")

    import news_intel.jobs.daily as daily_module
    monkeypatch.setattr(daily_module.TelegramSender, "send", _boom)

    with pytest.raises(RuntimeError, match="simulated network failure"):
        run_daily(config, conn, chat_id=555, dry_run=False, now=_NOW)

    row = conn.execute("SELECT * FROM deliveries").fetchone()
    assert row is not None
    assert row["status"] == "attempting"

    monkeypatch.undo()
    second = run_daily(config, conn, chat_id=555, dry_run=False, now=_NOW)
    assert second.fail_closed is True
    assert "already been attempted" in second.fail_reason

    count = conn.execute("SELECT COUNT(*) AS n FROM deliveries").fetchone()["n"]
    assert count == 1


def test_run_daily_idempotency_key_is_distinct_from_canary_scheme(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A qualifying story", category="AI & Semiconductors")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.idempotency_key.startswith("daily:")
    assert not summary.idempotency_key.startswith("daily_canary:")


# --- fail-closed: category / URL / allowlist --------------------------------


def test_run_daily_excludes_bad_category_before_selection_not_via_fail_closed(config, conn):
    """2026-08-11: category/URL validation now happens at candidate-pool
    filtering (selection order step 1), before dedup/coverage/fill -- so a
    single bad-category candidate with no other qualified events produces
    an honest empty digest, not a fail-closed error (the fail-closed path
    is reserved for a bad item that reaches rendering some other way; see
    test_run_daily_final_validation_still_catches_a_bypassed_bad_item)."""
    _seed_event(conn, config, event_id="E-1", title="A story", category="Unclassified")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.fail_closed is False
    assert summary.top3_count == 0
    assert summary.quick_brief_count == 0
    assert "No qualifying developments" in summary.rendered_text


def test_run_daily_excludes_missing_url_before_selection_not_via_fail_closed(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A story", category="AI & Semiconductors", url="")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.fail_closed is False
    assert summary.top3_count == 0
    assert summary.quick_brief_count == 0
    assert "No qualifying developments" in summary.rendered_text


def test_run_daily_final_validation_still_catches_a_bypassed_bad_item(config, conn, monkeypatch):
    """Defense in depth: even if a bad-category/no-URL item somehow reached
    selection (bypassing the step-1 pre-filter, e.g. a future regression),
    the pre-send fail-closed check must still independently catch it."""
    _seed_event(conn, config, event_id="E-1", title="A story", category="AI & Semiconductors")

    import news_intel.jobs.daily as daily_module
    from news_intel.pipeline.daily_selection import SelectionResult, Story

    real_select = daily_module.select_daily_stories

    def _bypassed(candidates, **kwargs):
        result = real_select(candidates, **kwargs)
        bad = dict(result.top_stories[0].primary)
        bad["category"] = "Unclassified"
        return SelectionResult(top_stories=[Story(primary=bad)], more_to_know=[])

    monkeypatch.setattr(daily_module, "select_daily_stories", _bypassed)
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.fail_closed is True
    assert "excluded category" in summary.fail_reason


def test_run_daily_fails_closed_when_not_exactly_one_allowed_chat(config, conn, monkeypatch):
    _seed_event(conn, config, event_id="E-1", title="A story", category="AI & Semiconductors")
    monkeypatch.setenv("TELEGRAM_ALLOWED_CHAT_IDS", "555,556")
    reset_config_cache()
    cfg = load_config(config_dir=config.config_dir, data_dir=config.data_dir)
    summary = run_daily(cfg, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.fail_closed is True
    assert "exactly one allowlisted" in summary.fail_reason
    reset_config_cache()


# --- [DAILY] heading -----------------------------------------------------


def test_run_daily_adds_daily_prefix_when_chat_id_given(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A story", category="AI & Semiconductors")
    summary = run_daily(config, conn, chat_id=555, dry_run=True, now=_NOW)
    assert summary.rendered_text.startswith("[DAILY]\n")
    assert "CANARY" not in summary.rendered_text


def test_run_daily_no_prefix_in_pure_preview_without_chat_id(config, conn):
    _seed_event(conn, config, event_id="E-1", title="A story", category="AI & Semiconductors")
    summary = run_daily(config, conn, chat_id=None, dry_run=True, now=_NOW)
    assert not summary.rendered_text.startswith("[DAILY]")


# --- CLI pre-start-date skip -------------------------------------------------


def test_cli_production_daily_skips_before_start_date_with_zero_telegram_contact(config, conn, monkeypatch):
    import news_intel.cli as cli_module

    monkeypatch.setattr(cli_module, "_load", lambda: (config, conn))
    called = {"run_daily": False}

    def _spy(*args, **kwargs):
        called["run_daily"] = True
        raise AssertionError("run_daily must not be called before the start date")

    monkeypatch.setattr(cli_module, "run_daily", _spy)
    monkeypatch.setattr(
        cli_module, "datetime",
        type("_FixedDatetime", (), {"now": staticmethod(lambda tz=None: _NOW)}),
    )

    runner = CliRunner()
    result = runner.invoke(cli_module.app, ["production", "daily", "--start-date", "2099-01-01"])

    assert result.exit_code == 0
    assert "skipping, no Telegram contact" in result.stdout
    assert called["run_daily"] is False
