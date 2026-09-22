"""Daily brief job (design doc sections 3.4, 4.2, 8.4). Runs Mon-Sat 22:00
America/Los_Angeles via systemd timer. Selection, scoring, and delivery
policy are deterministic; only composition text comes from the LLM, with a
documented deterministic fallback if it is unavailable."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery.daily_production_formatter import render_production_daily
from news_intel.delivery.daily_production_formatter_zh import render_production_daily_zh
from news_intel.delivery.formatter import render_daily
from news_intel.delivery.splitter import split_at_sections
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, mark_attempting
from news_intel.delivery.telegram_html import PARSE_MODE as TELEGRAM_PARSE_MODE
from news_intel.delivery.telegram_html import TelegramHTMLError, escape_text, plain_to_html
from news_intel.delivery.telegram_html import validate as validate_telegram_html
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
from news_intel.pipeline import freshness
from news_intel.pipeline.daily_selection import SelectionResult, select_daily_stories
from news_intel.pipeline.editorial_enrichment import (
    EDITORIAL_RUN_BUDGET_SECONDS,
    build_bounded_provider,
    enrich_story,
)
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.event_integrity import is_dashboard_like, is_low_consequence_event_type
from news_intel.pipeline.event_integrity_zh import is_low_consequence_event_type_zh
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

_EDITION_DAILY_LABELS = {"en_global": "[GLOBAL DAILY]", "zh_cn": "[中文日报]"}

# 2026-08-29 production incident: a daily brief may span at most this many
# Telegram messages. Two parts is a normal, readable daily; more than that
# means the digest is anomalously large (a runaway candidate window, a
# renderer defect) and is worth failing closed on rather than flooding the
# recipient. delivery/telegram.py's sender does the actual splitting and
# "(i/N)" labelling -- this only bounds it.
_MAX_DAILY_MESSAGE_PARTS = 2


def _daily_idempotency_key(edition_id: str, date_iso: str, chat_suffix: str) -> str:
    """en_global keeps its pre-existing key format (no edition segment, so
    already-created dates' idempotency is unchanged); zh_cn includes the
    edition explicitly so an English and a Chinese delivery can never
    collide merely because they share a bot/recipient."""
    if edition_id == "en_global":
        return f"daily:{date_iso}:{chat_suffix or 'no-chat'}"
    return f"daily:{edition_id}:{date_iso}:{chat_suffix or 'no-chat'}"


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
    better story exists -- see pipeline/event_integrity.py.

    2026-08-21 multi-edition dispatch: the English gate's phrase lists are
    all English words and never match Chinese text -- a zh_cn candidate
    previously received zero event-type/consequence filtering beyond
    classify_relevance's noise_exclusions and render-readiness (real
    defect: a conference-appearance story and an industry-award-list
    story both reached selection with nothing behind the headline). See
    pipeline/event_integrity_zh.py for the Chinese-language gate."""
    primary = item["sources"][0] if item.get("sources") else {}
    title, excerpt = item.get("title", ""), primary.get("excerpt")
    if item.get("edition_id") == "zh_cn":
        return not is_low_consequence_event_type_zh(title, excerpt)
    return not is_low_consequence_event_type(title, excerpt)


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

# 2026-08-17 freshness repair: the prior version of _is_fresh unconditionally
# treated a blocked/unverifiable origin as fresh ("if not origin_published_at:
# return True") -- exactly how an aggregator "republishing" a genuinely old,
# paywalled article (with today's own roundup pubDate) could silently pass as
# breaking news. Replaced with the explicit verified_fresh/verified_stale/
# bounded_unverified/insufficient_freshness_evidence state machine in
# pipeline/freshness.py -- see that module's docstring for the full
# safeguard list bounded_unverified requires. This state is purely an
# internal gate; never shown to the reader.


def _is_fresh(conn: sqlite3.Connection, item: dict, window_end: datetime) -> bool:
    first_seen_raw = item.get("first_seen_at")
    first_seen_at = None
    if first_seen_raw:
        try:
            first_seen_at = datetime.fromisoformat(first_seen_raw)
        except ValueError:
            first_seen_at = None
        else:
            if first_seen_at.tzinfo is None:
                first_seen_at = first_seen_at.replace(tzinfo=UTC)
    assessment = freshness.assess_freshness(
        conn, item, window_end=window_end, event_first_seen_at=first_seen_at
    )
    return assessment.is_fresh


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


def _tomorrow_calendar_items(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, *, edition_id: str = "en_global"
) -> list[dict]:
    """Only events on the next America/Los_Angeles calendar date relative to
    the digest's own Pacific date -- not a flat lookahead window (2026-08-12
    repair: the prior 36-hour sliding window let a same-week-but-not-
    tomorrow release, e.g. PPI two days out, leak into the digest).

    2026-08-21 repair: scoped to edition_id -- previously unscoped, so the
    identical en_global-only BLS/BEA calendar item rendered verbatim in
    zh_cn's "明日关注" too, regardless of relevance to a Chinese-language
    audience."""
    tz = ZoneInfo(config.policy.timezone)
    pt_now = now.astimezone(tz)
    target_date = (pt_now + timedelta(days=1)).date()
    items = []
    for row in upcoming(conn, within_hours=60, now=now, edition_id=edition_id):
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
    skipped_monthly_slot: bool = False
    fail_closed: bool = False
    fail_reason: str | None = None


def _select_events(
    conn: sqlite3.Connection, config: AppConfig, now: datetime, *, edition_id: str = "en_global"
) -> tuple[datetime, SelectionResult]:
    """Selection order: (0) cross-day novelty -- drop a candidate matching
    an already-delivered story from the last ~week of daily digests unless
    it carries a proven material update; (1) qualification
    (qualified_events_since -- score/eligibility/source-EXISTS, unchanged
    threshold) and source validation (category/human-readable URL, filtered
    here rather than only checked after the fact); (2)-(4) story-family
    dedup, cross-category coverage, and capped score-based fill all happen
    inside select_daily_stories(). Never lowers the qualification threshold
    and never pads a category with an unqualified candidate.

    Multi-edition: every DB read here is scoped to `edition_id` --
    qualified_events_since, last_digest_window_end, recent_digest_events
    all filter on that edition's own rows only, and generic_terms/
    known_entities come from that edition's own vocabulary
    (config.topics_for). A zh_cn run can never see en_global's candidates,
    prior-coverage novelty history, or window boundaries, and vice versa --
    this is what makes "cross-edition novelty must never suppress an event
    merely because the other edition reported it" true by construction,
    not just by convention."""
    thresholds = config.policy.scoring.thresholds
    window_start = last_digest_window_end(conn, "daily", edition_id=edition_id) or (now - timedelta(hours=24))
    qualified_rows = qualified_events_since(
        conn, window_start=window_start, min_score=thresholds.daily_quick_brief, edition_id=edition_id
    )

    candidates = [event_as_digest_dict(conn, r) for r in qualified_rows]
    candidates = [
        c for c in candidates
        if c["category"] not in _EXCLUDED_CATEGORIES
        and _has_human_readable_url(c)
        and _is_discrete_event(c)
        and _has_material_consequence(c)
        and _is_fresh(conn, c, now)
    ]

    event_topics = config.topics_for(edition_id)
    generic_terms = set(event_topics.trend_identity.generic_anchor_terms)
    entities_vocab = known_entities(event_topics.groups)
    prior_stories = recent_digest_events(
        conn, since=now - timedelta(days=_NOVELTY_LOOKBACK_DAYS), until=window_start, kind="daily",
        edition_id=edition_id,
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
    edition_id: str = "en_global",
) -> DailySummary:
    now = now or datetime.now(UTC)
    edition = config.editions.get(edition_id)

    now_pt_date = now.astimezone(ZoneInfo(edition.schedule.timezone)).date()
    pt_weekday = now_pt_date.weekday()
    # 2026-09-07 production incident: these two skips were UNCONDITIONAL --
    # daily stood aside for a weekly brief on Sunday and a monthly brief on
    # the first Monday of the month. Both of those timers are disabled for
    # this release, so instead of being replaced the digest was simply never
    # produced: Sep 6 (Sunday) and Sep 7 (first Monday) delivered nothing for
    # EITHER edition, while systemd recorded "success" in about one second.
    # Standing aside is only correct when something is actually going to take
    # the slot, so it is now explicit per-edition policy (default False).
    if pt_weekday == 6 and edition.schedule.weekly_replaces_daily:
        return DailySummary(
            "", 0, 0, 0, False, "", "", True, False, skipped_sunday=True
        )
    if pt_weekday == 0 and now_pt_date.day <= 7 and edition.schedule.monthly_replaces_daily:
        return DailySummary(
            "", 0, 0, 0, False, "", "", True, False, skipped_monthly_slot=True
        )

    # Edition-scoped job lock -- a zh_cn daily run must never be blocked by
    # (or block) an en_global daily run, and vice versa.
    lock_name = "daily" if edition_id == "en_global" else f"daily_{edition_id}"
    if not db.acquire_job_lock(conn, lock_name):
        return DailySummary("", 0, 0, 0, False, "", "", True, False, skipped_lock=True)

    # 2026-08-29 production incident: daily was the only job recording its
    # run with edition_id=None (weekly_production/monthly_production always
    # passed it), so en_global and zh_cn runs were indistinguishable in
    # job_runs -- during this incident the en_global failures and the zh_cn
    # successes were interleaved under an identical job_name='daily',
    # edition_id=NULL, and could only be told apart by correlating
    # timestamps against the journal.
    run_id = job_runs.start(conn, "daily", edition_id=edition_id)
    try:
        window_start, selection = _select_events(conn, config, now, edition_id=edition_id)
        top3 = [s.primary for s in selection.top_stories]
        quick = [s.primary for s in selection.more_to_know]
        calendar_items = _tomorrow_calendar_items(conn, config, now, edition_id=edition_id)

        pt_now = now.astimezone(ZoneInfo(edition.schedule.timezone))
        date_label = pt_now.strftime("%b %-d")
        date_iso = pt_now.strftime("%Y-%m-%d")

        chat_suffix = ""
        if chat_id is not None:
            from news_intel.delivery.telegram import chat_id_hash

            chat_suffix = chat_id_hash(chat_id)
        idempotency_key = _daily_idempotency_key(edition_id, date_iso, chat_suffix)

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
            # whole-digest path renders PLAIN text: converted to Telegram
            # HTML by escaping it exactly once, so every daily message has
            # one format and one parse mode (delivery/telegram_html.py)
            body = plain_to_html(render_daily(date_label, output.structured))
            model_used = getattr(provider, "model", "llm")
        except LLMUnavailableError:
            # Multi-edition: selection/ranking/render-readiness are fully
            # shared core (identical SelectionResult either way) -- only
            # the renderer differs.
            #
            # 2026-08-24: bounded story-level editorial layer. Only
            # attempted when a provider is actually configured -- when
            # nothing is configured (build_bounded_provider returns None),
            # every Story.editorial stays None and both renderers fall
            # straight through to their existing, already-hardened
            # deterministic why_it_matters()/chinese_takeaway() functions,
            # UNCHANGED from before this layer existed. This is
            # deliberately separate from compose_daily above (a single
            # whole-digest LLM call this project has never actually relied
            # on in production) -- per-story enrichment, each result
            # independently validated against its own evidence packet,
            # is the mechanism this layer commits to.
            bounded_provider = build_bounded_provider(config)
            if bounded_provider is not None:
                # 2026-08-29: whole-run deadline, not just a per-call
                # timeout -- see EDITORIAL_RUN_BUDGET_SECONDS. Stories
                # reached after the budget is gone take the source-grounded
                # floor without a provider call, so the editorial layer can
                # never push this job past its systemd TimeoutStartSec.
                editorial_deadline = time.monotonic() + EDITORIAL_RUN_BUDGET_SECONDS
                for story in selection.top_stories:
                    story.editorial = enrich_story(
                        story.primary, story.secondary, story.category, story.primary.get("entities"),
                        edition_id=edition_id, update_note=story.update_note, provider=bounded_provider,
                        deadline=editorial_deadline,
                    )
            if edition_id == "zh_cn":
                body = render_production_daily_zh(selection, date_label, calendar_items)
            else:
                body = render_production_daily(selection, date_label, calendar_items)
            fallback_used = True
            validated_stories = [
                s for s in selection.top_stories if s.editorial and s.editorial.source == "llm_validated"
            ]
            if validated_stories:
                model_used = validated_stories[0].editorial.model_used or "llm"
                fallback_used = False

        # Production heading: only stamped when delivery is being
        # contemplated (chat_id given) -- a pure content-only preview
        # (chat_id=None) stays unprefixed. Edition-distinct reader-facing
        # header ([GLOBAL DAILY]/[中文日报]).
        header = _EDITION_DAILY_LABELS.get(edition_id, "[GLOBAL DAILY]")
        rendered_text = f"{escape_text(header)}\n{body}" if chat_id is not None else body

        if chat_id is not None:
            # 2026-08-29 production incident: this gate previously rejected
            # ANY digest over the single-message limit, even though
            # delivery/telegram.py's sender already splits at blank-line
            # section boundaries and labels the parts "(1/2)"/"(2/2)" --
            # the exact mechanism weekly_formatter.py and monthly_
            # formatter.py have always relied on. The result was a
            # COMPOUNDING permanent stall for en_global: fail-closed
            # returns before record_digest(), so last_digest_window_end
            # never advanced, so the next run's candidate window was one
            # day wider, so the digest was longer, so it fail-closed
            # again. Three consecutive nights (Aug 27/28/29) produced
            # 4644/4455/4910-char digests and zero English deliveries,
            # with no path to self-recovery.
            #
            # A digest that merely needs splitting is not unsafe -- being
            # truncated or garbled would be, and the splitter does
            # neither. Fail closed only when the split would exceed a
            # bounded number of parts, which is the real "something is
            # wrong with this digest" signal.
            # 2026-09-22: never contemplate sending markup Telegram would
            # reject (or display literally) -- checked before the durable
            # attempt marker, so a bad render can be fixed and re-run
            try:
                validate_telegram_html(rendered_text)
            except TelegramHTMLError as exc:
                reason = f"rendered digest is not valid Telegram HTML: {exc}"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return DailySummary(
                    "", 0, 0, 0, False, "", idempotency_key, True, False, fail_closed=True, fail_reason=reason,
                )
            max_chars = config.policy.digests.telegram_hard_limit_chars
            parts = split_at_sections(rendered_text, max_chars)
            if len(parts) > _MAX_DAILY_MESSAGE_PARTS:
                reason = (
                    f"rendered digest is {len(rendered_text)} chars and would split into {len(parts)} "
                    f"messages, exceeding the {_MAX_DAILY_MESSAGE_PARTS}-part limit"
                )
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
                        # 2026-09-22 incident: this was `"HTML" if fallback_used
                        # else None`, so every brief with a validated LLM story
                        # (en since Sep 1, zh from Sep 21) went out with no
                        # parse mode and readers saw raw <a href> and &#x27;.
                        # The body is ALWAYS Telegram HTML now (see above).
                        parse_mode=TELEGRAM_PARSE_MODE,
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
            edition_id=edition_id,
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
        db.release_job_lock(conn, lock_name)
