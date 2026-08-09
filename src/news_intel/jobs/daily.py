"""Daily brief job (design doc sections 3.4, 4.2, 8.4). Runs Mon-Sat 22:00
America/Los_Angeles via systemd timer. Selection, scoring, and delivery
policy are deterministic; only composition text comes from the LLM, with a
documented deterministic fallback if it is unavailable."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery.formatter import render_daily
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, mark_attempting
from news_intel.event_repo import (
    event_as_digest_dict,
    last_digest_window_end,
    qualified_events_since,
    record_digest,
)
from news_intel.jobs import job_runs
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.fallback import fallback_daily_digest
from news_intel.llm.provider import get_provider
from news_intel.models import DailyDigestInput
from news_intel.pipeline.select import select_diverse_events
from news_intel.scheduled_repo import upcoming
from news_intel.settings import AppConfig

# Production hardening (2026-08-09, promoted from the bounded daily canary):
# any event reaching the rendered digest must have a real category and a
# mapped source URL -- defense in depth on top of qualified_events_since's
# own EXISTS(event_sources) filter, mirroring jobs/daily_canary.py.
_EXCLUDED_CATEGORIES = {"noise", "Unclassified", "uncategorized", None}


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


def _select_events(conn: sqlite3.Connection, config: AppConfig, now: datetime):
    thresholds = config.policy.scoring.thresholds
    digests_policy = config.policy.digests
    window_start = last_digest_window_end(conn, "daily") or (now - timedelta(hours=24))
    qualified = qualified_events_since(conn, window_start=window_start, min_score=thresholds.daily_quick_brief)

    top3, quick = select_diverse_events(
        qualified,
        max_per_family=digests_policy.max_events_per_origin_family,
        min_events=digests_policy.daily_min_events,
        max_events=digests_policy.daily_max_events,
        top3_threshold=thresholds.daily_top3,
    )
    return window_start, top3, quick


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
        window_start, top3_rows, quick_rows = _select_events(conn, config, now)
        top3 = [event_as_digest_dict(conn, r) for r in top3_rows]
        quick = [event_as_digest_dict(conn, r) for r in quick_rows]
        calendar_items = [
            {"title": row["title"], "scheduled_at": row["scheduled_at"]} for row in upcoming(conn, now=now)
        ]

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
            fallback_input = DailyDigestInput(
                date_label=date_label,
                top3_candidates=top3,
                quick_brief_candidates=quick,
                calendar_candidates=calendar_items,
            )
            fallback_output = fallback_daily_digest(fallback_input, date_label)
            body = fallback_output.rendered_text
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
