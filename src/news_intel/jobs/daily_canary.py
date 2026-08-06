"""Bounded three-delivery daily canary.

Gate B approved the deterministic-fallback daily digest path and a single
authorized Telegram test send, but flagged one disclosed weekly-trend
residual (the "Ilya Sutskever" person-anchor gap) that blocks weekly
delivery specifically. It does not touch daily selection/rendering, so the
user authorized exactly three real daily sends -- on three consecutive
Pacific dates -- as a bounded canary before considering daily delivery for
normal operation.

This module is deliberately independent of jobs/daily.py's own 'daily'
digest kind/window tracking: it uses its own kind ("daily_canary") for
window bookkeeping (event_repo.last_digest_window_end/record_digest) and
delivery idempotency (delivery.telegram.mark_attempting), so a canary run
can never be confused with (or interfere with) a future real daily job.

Every fail-closed condition below is checked *before* any Telegram contact
and, on failure, causes: no send, a job_runs failure record, and a return
to the caller so collection continues untouched until the next scheduled
date. Nothing here is retried automatically or manually -- mark_attempting
durably pre-marks the Pacific date as attempted before the network call, so
even a send that raises leaves behind a record that blocks any later retry
for that same date (see delivery.telegram.mark_attempting's docstring)."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from news_intel.delivery.formatter import render_daily
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, chat_id_hash, mark_attempting
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

CANARY_KIND = "daily_canary"

# Only these are qualifying categories for the canary -- see
# pipeline/scoring.py's _CATEGORY_LABELS. "noise" and "Unclassified" are
# real stored category values (not filtered by category anywhere upstream:
# event_repo.qualified_events_since only filters by score/eligibility-time),
# and event_as_digest_dict falls back to "uncategorized" for a NULL
# category -- all three are excluded here as a defense-in-depth check
# independent of whatever the score threshold happens to do.
_EXCLUDED_CATEGORIES = {"noise", "Unclassified", "uncategorized", None}


@dataclass
class CanarySummary:
    ran_send_attempt: bool
    sent: bool
    fail_closed: bool
    fail_reason: str | None
    canary_index: int
    pt_date: str
    rendered_text: str
    character_count: int
    event_count: int
    idempotency_key: str
    message_ids: list[str] = field(default_factory=list)


def _fail_closed(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    reason: str,
    canary_index: int,
    pt_date: str,
    idempotency_key: str,
) -> CanarySummary:
    job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason, "canary_index": canary_index})
    return CanarySummary(
        ran_send_attempt=False,
        sent=False,
        fail_closed=True,
        fail_reason=reason,
        canary_index=canary_index,
        pt_date=pt_date,
        rendered_text="",
        character_count=0,
        event_count=0,
        idempotency_key=idempotency_key,
    )


def run_daily_canary(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    canary_index: int,
    now: datetime | None = None,
) -> CanarySummary:
    now = now or datetime.now(UTC)
    tz = ZoneInfo(config.policy.timezone)
    pt_now = now.astimezone(tz)
    date_iso = pt_now.strftime("%Y-%m-%d")
    date_label = pt_now.strftime("%b %-d")

    run_id = job_runs.start(conn, CANARY_KIND)

    # A placeholder idempotency key (chat hash filled in once the allowlist
    # check below confirms a single destination) -- fail_closed() below
    # needs *a* key for the job_runs/report trail even for checks that fail
    # before a real chat_id is established.
    provisional_key = f"{CANARY_KIND}:{date_iso}:unresolved-chat"

    if not (1 <= canary_index <= 3):
        return _fail_closed(
            conn, run_id,
            reason=f"canary_index {canary_index} is outside the bounded [1, 3] window",
            canary_index=canary_index, pt_date=date_iso, idempotency_key=provisional_key,
        )

    allowed = config.secrets.allowed_chat_id_list
    if len(allowed) != 1:
        return _fail_closed(
            conn, run_id,
            reason=f"expected exactly one allowlisted Telegram chat, found {len(allowed)}",
            canary_index=canary_index, pt_date=date_iso, idempotency_key=provisional_key,
        )
    chat_id = allowed[0]
    idempotency_key = f"{CANARY_KIND}:{date_iso}:{chat_id_hash(chat_id)}"

    thresholds = config.policy.scoring.thresholds
    digests_policy = config.policy.digests
    window_start = last_digest_window_end(conn, CANARY_KIND) or (now - timedelta(hours=24))
    qualified = qualified_events_since(conn, window_start=window_start, min_score=thresholds.daily_quick_brief)

    top3_rows, quick_rows = select_diverse_events(
        qualified,
        max_per_family=digests_policy.max_events_per_origin_family,
        min_events=digests_policy.daily_min_events,
        max_events=digests_policy.daily_max_events,
        top3_threshold=thresholds.daily_top3,
    )
    top3 = [event_as_digest_dict(conn, r) for r in top3_rows]
    quick = [event_as_digest_dict(conn, r) for r in quick_rows]
    all_selected = top3 + quick

    for item in all_selected:
        if item["category"] in _EXCLUDED_CATEGORIES:
            return _fail_closed(
                conn, run_id,
                reason=f"selected event {item['event_id']} has excluded category {item['category']!r}",
                canary_index=canary_index, pt_date=date_iso, idempotency_key=idempotency_key,
            )
        if not any(s.get("url") for s in item["sources"]):
            return _fail_closed(
                conn, run_id,
                reason=f"selected event {item['event_id']} has no mapped source URL",
                canary_index=canary_index, pt_date=date_iso, idempotency_key=idempotency_key,
            )

    label = f"[DAILY CANARY {canary_index}/3]"

    if not all_selected:
        rendered_text = f"{label} {date_label}\n\nNo qualifying developments were found in the last 24 hours."
        model_used = "none (no qualifying events)"
        fallback_used = False
    else:
        calendar_items = [
            {"title": row["title"], "scheduled_at": row["scheduled_at"]} for row in upcoming(conn, now=now)
        ]
        digest_input = DailyDigestInput(
            date_label=date_label,
            top3_candidates=top3,
            quick_brief_candidates=quick,
            calendar_candidates=calendar_items,
        )
        provider = get_provider(config)
        model_used = "none (deterministic fallback)"
        fallback_used = False
        try:
            output = provider.compose_daily(digest_input)
            for ev, source_dict in zip(output.structured.get("top3", []), top3, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            for ev, source_dict in zip(output.structured.get("quick_briefs", []), quick, strict=False):
                ev.setdefault("sources", [s["publisher"] for s in source_dict["sources"]])
            body = render_daily(date_label, output.structured)
            model_used = getattr(provider, "model", "llm")
        except LLMUnavailableError:
            fallback_output = fallback_daily_digest(digest_input, date_label)
            body = fallback_output.rendered_text
            fallback_used = True
        rendered_text = f"{label}\n{body}"

    max_chars = digests_policy.telegram_hard_limit_chars
    if len(rendered_text) > max_chars:
        return _fail_closed(
            conn, run_id,
            reason=f"rendered output is {len(rendered_text)} chars, exceeds the {max_chars}-char single-message limit",
            canary_index=canary_index, pt_date=date_iso, idempotency_key=idempotency_key,
        )

    if not mark_attempting(conn, idempotency_key, chat_id, CANARY_KIND):
        return _fail_closed(
            conn, run_id,
            reason=f"Pacific date {date_iso} has already been attempted for this destination -- not retrying",
            canary_index=canary_index, pt_date=date_iso, idempotency_key=idempotency_key,
        )

    sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
    sent = False
    message_ids: list[str] = []
    try:
        result = asyncio.run(
            sender.send(
                conn,
                chat_id=chat_id,
                text=rendered_text,
                kind=CANARY_KIND,
                idempotency_key=idempotency_key,
                max_chars=max_chars,
                dry_run=False,
            )
        )
        sent = not result.dry_run
        message_ids = result.message_ids
    except ChatNotAllowedError as exc:
        job_runs.finish(conn, run_id, "failed", {"error": str(exc), "canary_index": canary_index})
        return CanarySummary(
            ran_send_attempt=True, sent=False, fail_closed=True, fail_reason=str(exc),
            canary_index=canary_index, pt_date=date_iso, rendered_text=rendered_text,
            character_count=len(rendered_text), event_count=len(all_selected),
            idempotency_key=idempotency_key,
        )
    except Exception as exc:  # noqa: BLE001 -- the mark_attempting() pre-mark above is what
        # guarantees no retry, regardless of what kind of send failure this is; the failure
        # must still surface (job_runs "failed"), not be swallowed.
        job_runs.finish(conn, run_id, "failed", {"error": str(exc), "canary_index": canary_index})
        raise

    record_digest(
        conn,
        kind=CANARY_KIND,
        window_start=window_start,
        window_end=now,
        rendered_text=rendered_text,
        model_used=model_used,
        fallback_used=fallback_used,
        event_ids_top3=[t["event_id"] for t in top3],
        event_ids_quick=[q["event_id"] for q in quick],
    )
    job_runs.finish(
        conn, run_id, "success",
        {"canary_index": canary_index, "sent": sent, "event_count": len(all_selected)},
    )

    return CanarySummary(
        ran_send_attempt=True,
        sent=sent,
        fail_closed=False,
        fail_reason=None,
        canary_index=canary_index,
        pt_date=date_iso,
        rendered_text=rendered_text,
        character_count=len(rendered_text),
        event_count=len(all_selected),
        idempotency_key=idempotency_key,
        message_ids=message_ids,
    )
