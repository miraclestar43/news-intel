"""Daily brief job (design doc sections 3.4, 4.2, 8.4). Runs Mon-Sat 22:00
America/Los_Angeles via systemd timer. Selection, scoring, and delivery
policy are deterministic; only composition text comes from the LLM, with a
documented deterministic fallback if it is unavailable."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery.daily_production_formatter import render_production_daily
from news_intel.delivery.formatter import render_daily
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, mark_attempting
from news_intel.event_repo import (
    event_as_digest_dict,
    last_digest_window_end,
    qualified_events_since,
    recent_digest_events,
    record_digest,
)
from news_intel.jobs import job_runs
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.provider import get_provider
from news_intel.models import DailyDigestInput
from news_intel.pipeline.daily_selection import SelectionResult, select_daily_stories
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.event_integrity import is_dashboard_like, is_low_consequence_event_type
from news_intel.scheduled_repo import upcoming
from news_intel.settings import AppConfig

# Production hardening (2026-08-09, promoted from the bounded daily canary):
# any event reaching the rendered digest must have a real category and a
# mapped source URL -- defense in depth on top of qualified_events_since's
# own EXISTS(event_sources) filter, mirroring jobs/daily_canary.py.
_EXCLUDED_CATEGORIES = {"noise", "Unclassified", "uncategorized", None}

# 2026-08-12 content-quality repair: "a valid canonical human-readable
# source URL" -- a bare domain with no path, or a machine-feed endpoint, is
# never a displayable article link. All current event-candidate adapters
# already store real article URLs in practice; this is a defensive,
# explicit qualification gate, not a behavior change for real data.
_MACHINE_FEED_SUFFIXES = (".ics", ".json", ".xml", ".rss")


def _has_human_readable_url(item: dict) -> bool:
    for source in item.get("sources", []):
        url = source.get("url")
        if not url:
            continue
        parsed = urlparse(url)
        if parsed.path and parsed.path != "/" and not parsed.path.lower().endswith(_MACHINE_FEED_SUFFIXES):
            return True
    return False


def _is_discrete_event(item: dict) -> bool:
    """2026-08-13 repair: a dashboard/"latest numbers" landing page is not
    a discrete news event -- see pipeline/event_integrity.py. Checked
    against the primary source's own title/excerpt (the item's own
    primary_title mirrors the primary source's title)."""
    primary = item["sources"][0] if item.get("sources") else {}
    return not is_dashboard_like(item.get("title", ""), primary.get("excerpt"))


def _has_material_consequence(item: dict) -> bool:
    """Event-type/consequence gate (stage 2 of selection): a recognition/
    award/ranking story, a tutorial/builder-guide/promotional post, or an
    enforcement action against a single named individual must not qualify
    for the digest merely because the publisher is authoritative or no
    better story exists -- see pipeline/event_integrity.py."""
    primary = item["sources"][0] if item.get("sources") else {}
    return not is_low_consequence_event_type(item.get("title", ""), primary.get("excerpt"))


# 2026-08-16 freshness repair: a discovery aggregator (Techmeme) can
# "republish" -- carry in its own daily roundup, with today's own pubDate
# -- an article that is genuinely several days old. Exact regression: a
# Point2 Series B story that SDxCentral itself published 2026-08-10 was
# presented as 2026-08-15 news, because raw_items.published_at for a
# Techmeme-discovered item is Techmeme's own roundup timestamp, never the
# origin article's. Only fires when origin_published_at was actually
# verified by a live fetch of the origin page (adapters/rss.py's
# _fetch_origin_metadata, attempted at collection time); an unverifiable
# origin (paywalled/bot-blocked -- confirmed live against ft.com, which
# returns a 403 "Security Verification" page with no metadata at all -- or
# any non-aggregator source, which never attempts this fetch) falls back
# to the existing published_at-based window membership unchanged. Failing
# an unverifiable origin closed would incorrectly exclude genuinely fresh
# same-day stories from paywalled outlets (the Manus/FT and Nvidia/The
# Information stories in this same regression) purely because their
# publisher blocks metadata scraping -- verification failure is not
# evidence of staleness.
_STALE_ORIGIN_BUFFER = timedelta(hours=48)


def _is_fresh(item: dict, window_end: datetime) -> bool:
    primary = item["sources"][0] if item.get("sources") else {}
    origin_published_at = primary.get("origin_published_at")
    if not origin_published_at:
        return True
    try:
        origin_dt = datetime.fromisoformat(origin_published_at)
    except ValueError:
        return True
    if origin_dt.tzinfo is None:
        origin_dt = origin_dt.replace(tzinfo=UTC)
    return origin_dt >= (window_end - _STALE_ORIGIN_BUFFER)


# Cross-day novelty lookback (2026-08-12 repair): "at least the previous
# seven Pacific digest dates" -- 8 calendar days back from the eligibility
# window comfortably covers 7+ prior daily digests even accounting for a
# skipped Sunday (weekly replaces daily) or an autostop gap.
_NOVELTY_LOOKBACK_DAYS = 8

# TOMORROW TO WATCH: human-readable release pages for scheduled-calendar
# titles whose stored `url` is a machine-readable .ics feed (2026-08-12
# repair) -- the .ics is correct as ingestion input (scheduled_repo/
# jobs/collect.py) but must never be the reader-facing link. Only entries
# with a known mapping (or an already-human-readable stored URL) get a
# link; everything else renders as plain text rather than ever showing a
# machine-feed URL.
_CALENDAR_HUMAN_READABLE_URLS = {
    "Consumer Price Index": "https://www.bls.gov/schedule/news_release/cpi.htm",
    "Real Earnings": "https://www.bls.gov/schedule/news_release/realer.htm",
    "Producer Price Index": "https://www.bls.gov/schedule/news_release/ppi.htm",
}


def _calendar_display_url(title: str, stored_url: str | None) -> str | None:
    mapped = _CALENDAR_HUMAN_READABLE_URLS.get(title)
    if mapped:
        return mapped
    if stored_url and not stored_url.lower().endswith(_MACHINE_FEED_SUFFIXES):
        return stored_url
    return None


def _tomorrow_calendar_items(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> list[dict]:
    """Only events on the next America/Los_Angeles calendar date relative to
    the digest's own Pacific date -- not a flat lookahead window (2026-08-12
    repair: the prior 36-hour sliding window let a same-week-but-not-
    tomorrow release, e.g. PPI two days out, leak into the digest)."""
    tz = ZoneInfo(config.policy.timezone)
    pt_now = now.astimezone(tz)
    target_date = (pt_now + timedelta(days=1)).date()
    items = []
    for row in upcoming(conn, within_hours=60, now=now):
        try:
            scheduled = datetime.fromisoformat(row["scheduled_at"])
        except ValueError:
            continue
        if scheduled.astimezone(tz).date() != target_date:
            continue
        items.append(
            {
                "title": row["title"],
                "scheduled_at": row["scheduled_at"],
                "pacific_time_label": scheduled.astimezone(tz).strftime("%-I:%M %p PT"),
                "url": _calendar_display_url(row["title"], row["url"]),
            }
        )
    return items


@dataclass
class DailySummary:
    rendered_text: str
    character_count: int
    top3_count: int
    quick_brief_count: int
    fallback_used: bool
    model_used: str
    idempotency_key: str
    sent_dry_run: bool
    sent: bool
    skipped_lock: bool = False
    skipped_sunday: bool = False
    fail_closed: bool = False
    fail_reason: str | None = None


def _select_events(conn: sqlite3.Connection, config: AppConfig, now: datetime) -> tuple[datetime, SelectionResult]:
    """Selection order: (0) cross-day novelty -- drop a candidate matching
    an already-delivered story from the last ~week of daily digests unless
    it carries a proven material update; (1) qualification
    (qualified_events_since -- score/eligibility/source-EXISTS, unchanged
    threshold) and source validation (category/human-readable URL, filtered
    here rather than only checked after the fact); (2)-(4) story-family
    dedup, cross-category coverage, and capped score-based fill all happen
    inside select_daily_stories(). Never lowers the qualification threshold
    and never pads a category with an unqualified candidate."""
    thresholds = config.policy.scoring.thresholds
    window_start = last_digest_window_end(conn, "daily") or (now - timedelta(hours=24))
    qualified_rows = qualified_events_since(conn, window_start=window_start, min_score=thresholds.daily_quick_brief)

    candidates = [event_as_digest_dict(conn, r) for r in qualified_rows]
    candidates = [
        c for c in candidates
        if c["category"] not in _EXCLUDED_CATEGORIES
        and _has_human_readable_url(c)
        and _is_discrete_event(c)
        and _has_material_consequence(c)
        and _is_fresh(c, now)
    ]

    generic_terms = set(config.topics.trend_identity.generic_anchor_terms)
    entities_vocab = known_entities(config.topics.groups)
    prior_stories = recent_digest_events(
        conn, since=now - timedelta(days=_NOVELTY_LOOKBACK_DAYS), until=window_start, kind="daily"
    )
    result = select_daily_stories(
        candidates,
        generic_terms=generic_terms,
        known_entities=entities_vocab,
        alert_triggers=config.alert_triggers,
        prior_stories=prior_stories,
    )
    return window_start, result


def run_daily(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    chat_id: int | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
) -> DailySummary:
    now = now or datetime.now(UTC)

    pt_weekday = now.astimezone(ZoneInfo(config.policy.timezone)).weekday()
    if pt_weekday == 6:  # Sunday: the weekly brief replaces the daily send entirely
        return DailySummary(
            "", 0, 0, 0, False, "", "", True, False, skipped_sunday=True
        )

    if not db.acquire_job_lock(conn, "daily"):
        return DailySummary("", 0, 0, 0, False, "", "", True, False, skipped_lock=True)

    run_id = job_runs.start(conn, "daily")
    try:
        window_start, selection = _select_events(conn, config, now)
        top3 = [s.primary for s in selection.top_stories]
        quick = [s.primary for s in selection.more_to_know]
        calendar_items = _tomorrow_calendar_items(conn, config, now)

        pt_now = now.astimezone(ZoneInfo(config.policy.timezone))
        date_label = pt_now.strftime("%b %-d")
        date_iso = pt_now.strftime("%Y-%m-%d")

        chat_suffix = ""
        if chat_id is not None:
            from news_intel.delivery.telegram import chat_id_hash

            chat_suffix = chat_id_hash(chat_id)
        idempotency_key = f"daily:{date_iso}:{chat_suffix or 'no-chat'}"

        # Production hardening: whenever a chat_id is supplied (delivery is
        # actually being contemplated -- dry-run or real), fail closed on an
        # unsafe destination/selection before ever composing/sending,
        # exactly mirroring jobs/daily_canary.py's checks.
        if chat_id is not None:
            allowed = config.secrets.allowed_chat_id_list
            if len(allowed) != 1:
                reason = f"expected exactly one allowlisted Telegram chat, found {len(allowed)}"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return DailySummary(
                    "", 0, 0, 0, False, "", idempotency_key, True, False,
                    fail_closed=True, fail_reason=reason,
                )
            for item in top3 + quick:
                if item["category"] in _EXCLUDED_CATEGORIES:
                    reason = f"selected event {item['event_id']} has excluded category {item['category']!r}"
                    job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                    return DailySummary(
                        "", 0, 0, 0, False, "", idempotency_key, True, False,
                        fail_closed=True, fail_reason=reason,
                    )
                if not any(s.get("url") for s in item["sources"]):
                    reason = f"selected event {item['event_id']} has no mapped source URL"
                    job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                    return DailySummary(
                        "", 0, 0, 0, False, "", idempotency_key, True, False,
                        fail_closed=True, fail_reason=reason,
                    )

        provider = get_provider(config)
        model_used = "none (deterministic fallback)"
        fallback_used = False
        body: str

        try:
            digest_input = DailyDigestInput(
                date_label=date_label,
                top3_candidates=top3,
                quick_brief_candidates=quick,
                calendar_candidates=calendar_items,
            )
            output = provider.compose_daily(digest_input)
            for ev, source_dict in zip(output.structured.get("top3", []), top3, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            for ev, source_dict in zip(output.structured.get("quick_briefs", []), quick, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            body = render_daily(date_label, output.structured)
            model_used = getattr(provider, "model", "llm")
        except LLMUnavailableError:
            body = render_production_daily(selection, date_label, calendar_items)
            fallback_used = True

        # Production heading: only stamped when delivery is being
        # contemplated (chat_id given) -- a pure content-only preview
        # (chat_id=None) stays unprefixed.
        rendered_text = f"[DAILY]\n{body}" if chat_id is not None else body

        if chat_id is not None:
            max_chars = config.policy.digests.telegram_hard_limit_chars
            if len(rendered_text) > max_chars:
                reason = f"rendered digest is {len(rendered_text)} chars, exceeds {max_chars}"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return DailySummary(
                    "", 0, 0, 0, False, "", idempotency_key, True, False,
                    fail_closed=True, fail_reason=reason,
                )

        sent = False
        sent_dry_run = True
        if chat_id is not None:
            # Real (non-dry-run) sends are durably pre-marked before any
            # network call, so a send that raises still leaves a row behind
            # -- this Pacific date/destination is then never retried, even
            # by a later invocation. See delivery.telegram.mark_attempting.
            if not dry_run and not mark_attempting(conn, idempotency_key, chat_id, "daily"):
                reason = f"Pacific date {date_iso} has already been attempted for this destination -- not retrying"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return DailySummary(
                    rendered_text, len(rendered_text), len(top3), len(quick), fallback_used, model_used,
                    idempotency_key, True, False, fail_closed=True, fail_reason=reason,
                )

            sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
            try:
                result = asyncio.run(
                    sender.send(
                        conn,
                        chat_id=chat_id,
                        text=rendered_text,
                        kind="daily",
                        idempotency_key=idempotency_key,
                        max_chars=config.policy.digests.telegram_hard_limit_chars,
                        dry_run=dry_run,
                        # Only render_production_daily's output is HTML-escaped
                        # with well-formed <a> tags; the dormant LLM-composed
                        # path (render_daily) is plain text and must not be
                        # sent as HTML without its own escaping.
                        parse_mode="HTML" if fallback_used else None,
                    )
                )
                sent = not result.dry_run
                sent_dry_run = result.dry_run
            except ChatNotAllowedError:
                sent = False
                sent_dry_run = True

        record_digest(
            conn,
            kind="daily",
            window_start=window_start,
            window_end=now,
            rendered_text=rendered_text,
            model_used=model_used,
            fallback_used=fallback_used,
            event_ids_top3=[t["event_id"] for t in top3],
            event_ids_quick=[q["event_id"] for q in quick],
        )

        job_runs.finish(
            conn,
            run_id,
            "success",
            {"top3": len(top3), "quick_briefs": len(quick), "fallback_used": fallback_used},
        )

        return DailySummary(
            rendered_text=rendered_text,
            character_count=len(rendered_text),
            top3_count=len(top3),
            quick_brief_count=len(quick),
            fallback_used=fallback_used,
            model_used=model_used,
            idempotency_key=idempotency_key,
            sent_dry_run=sent_dry_run,
            sent=sent,
        )
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.release_job_lock(conn, "daily")
