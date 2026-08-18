"""Monthly intelligence brief: runs on the first Monday of each calendar
month, replacing that Monday's daily send (see jobs/daily.py's
skipped_monthly_slot guard), and synthesizes the complete PREVIOUS
calendar month -- never the four weeklies concatenated. Window bounds are
derived from calendar arithmetic (the previous whole Jan-1..Feb-1-style
month relative to `now`), not the exact instant `now` falls on, so repeated
calls within the same month always compute identical bounds -- the same
idempotency property jobs/weekly.py's week_window() relies on.

Deliberately deterministic-only (no LLM-attempt-then-fallback path like
jobs/weekly.py's compose_weekly/fallback_weekly_digest): LLM_BASE_URL is
unset in production today, so get_provider() always returns NullProvider
and weekly's LLM path always raises LLMUnavailableError in practice --
wiring an equivalent compose_monthly path here would add an unexercised
code path, not real behavior. delivery/monthly_formatter.py's
render_monthly() is this module's single composition path, mirroring
fallback_weekly_digest's shape (trend clusters, comparison, noise/signal,
next-period calendar) directly."""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery import monthly_delivery_repo
from news_intel.delivery.monthly_formatter import render_monthly, split_monthly
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, chat_id_hash
from news_intel.event_repo import add_digest_events, create_digest, events_in_window
from news_intel.jobs import job_runs
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.trends import TrendCluster, build_trend_clusters, select_top_clusters
from news_intel.pipeline.weekly import compare_with_prior_week, identify_noise_vs_signal, unresolved_stories
from news_intel.scheduled_repo import upcoming
from news_intel.settings import AppConfig

_SECTION_CATEGORIES = {"AI & Semiconductors", "Markets & Economy", "US / China / World"}
_MONTH_NAMES = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


@dataclass
class MonthlySummary:
    rendered_parts: list[str] = field(default_factory=list)
    character_count: int = 0
    trend_count: int = 0
    fallback_used: bool = True
    model_used: str = "none (deterministic fallback)"
    idempotency_key: str = ""
    sent_dry_run: bool = True
    sent: bool = False
    skipped_lock: bool = False
    skipped_duplicate: bool = False
    month_label: str = ""
    fail_closed: bool = False
    fail_reason: str | None = None


def is_first_monday(pt_date: date) -> bool:
    return pt_date.weekday() == 0 and pt_date.day <= 7


def month_window(now_pt: datetime) -> tuple[datetime, datetime]:
    """Stable bounds for the complete calendar month BEFORE the one
    containing `now_pt` -- e.g. a Sep 7 run covers Aug 1 00:00 -> Sep 1
    00:00, regardless of what day in September `now_pt` actually falls on."""
    tz = now_pt.tzinfo
    this_month_start = now_pt.date().replace(day=1)
    prior_month_end = this_month_start
    prior_month_start = (this_month_start - timedelta(days=1)).replace(day=1)
    return (
        datetime.combine(prior_month_start, dtime.min, tzinfo=tz),
        datetime.combine(prior_month_end, dtime.min, tzinfo=tz),
    )


def _prior_month_window(month_start: datetime) -> tuple[datetime, datetime]:
    """The month immediately before the one being reported on, for
    new/continuing/resolved trend comparison -- one month back from
    month_start, same width-one-calendar-month construction."""
    tz = month_start.tzinfo
    prior_start = (month_start.date() - timedelta(days=1)).replace(day=1)
    return (
        datetime.combine(prior_start, dtime.min, tzinfo=tz),
        datetime.combine(month_start.date(), dtime.min, tzinfo=tz),
    )


def _month_label(month_start: datetime) -> str:
    return f"{_MONTH_NAMES[month_start.month - 1]} {month_start.year}"


def _titles_for_events(conn: sqlite3.Connection, event_ids: list[str]) -> list[str]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(f"SELECT primary_title FROM events WHERE id IN ({placeholders})", event_ids).fetchall()
    return [r["primary_title"] for r in rows]


def _titles_for_rows(rows: list[sqlite3.Row]) -> list[str]:
    return [r["primary_title"] for r in rows]


def _find_existing_monthly_digest(conn: sqlite3.Connection, window_start: datetime, window_end: datetime):
    return conn.execute(
        "SELECT * FROM digests WHERE kind = 'monthly' AND window_start = ? AND window_end = ? "
        "AND generation_state = 'generated' ORDER BY created_at DESC LIMIT 1",
        (window_start.isoformat(), window_end.isoformat()),
    ).fetchone()


def _trend_dict(conn: sqlite3.Connection, cluster: TrendCluster, status: str) -> dict:
    return {
        "id": cluster.id,
        "category": cluster.category,
        "key_entities": cluster.key_entities,
        "event_ids": cluster.event_ids,
        "titles": _titles_for_events(conn, cluster.event_ids),
        "strength": cluster.strength,
        "status": status,
        "is_trend": cluster.is_trend,
        "shared_anchor": cluster.shared_anchor,
    }


def _monthly_idempotency_key(month_label_key: str, chat_suffix: str) -> str:
    return f"monthly:{month_label_key}:{chat_suffix or 'no-chat'}"


def run_monthly(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    chat_id: int | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    resume_authorized: bool = False,
) -> MonthlySummary:
    now = now or datetime.now(UTC)
    tz = ZoneInfo(config.policy.timezone)
    now_pt = now.astimezone(tz)

    if not db.acquire_job_lock(conn, "monthly"):
        return MonthlySummary(skipped_lock=True)

    run_id = job_runs.start(conn, "monthly")
    try:
        window_start, window_end = month_window(now_pt)
        prior_start, prior_end = _prior_month_window(window_start)

        month_label = _month_label(window_start)
        month_label_key = f"{window_start.year:04d}-{window_start.month:02d}"

        existing = _find_existing_monthly_digest(conn, window_start, window_end)
        if existing is not None:
            parts = split_monthly(
                existing["rendered_text"], month_label, config.policy.digests.telegram_hard_limit_chars
            )
            return MonthlySummary(
                rendered_parts=parts, character_count=len(existing["rendered_text"]),
                model_used=existing["model_used"], fallback_used=bool(existing["fallback_used"]),
                idempotency_key=_monthly_idempotency_key(month_label_key, "no-chat"), sent_dry_run=True,
                sent=False, skipped_duplicate=True, month_label=month_label,
            )

        known = known_entities(config.topics.groups)
        current_events = events_in_window(conn, window_start=window_start, window_end=window_end)
        prior_events = events_in_window(conn, window_start=prior_start, window_end=prior_end)

        trend_policy = config.topics.trend_identity
        min_event_score = config.policy.scoring.thresholds.weekly_store
        current_clusters = build_trend_clusters(
            conn, current_events, known, policy=trend_policy, min_event_score=min_event_score
        )
        prior_clusters = build_trend_clusters(
            conn, prior_events, known, policy=trend_policy, min_event_score=min_event_score
        )
        comparison = compare_with_prior_week(current_clusters, prior_clusters)
        selected_clusters = select_top_clusters(
            current_clusters, max_items=config.policy.digests.weekly_max_selected_items
        )

        thresholds = config.policy.scoring.thresholds
        noise, signal = identify_noise_vs_signal(
            current_events, signal_threshold=thresholds.daily_top3, noise_threshold=thresholds.weekly_store
        )
        unresolved = unresolved_stories(current_events)

        calendar_rows = upcoming(conn, within_hours=24 * 31, now=now)
        calendar_candidates = [
            {"title": r["title"], "scheduled_at": r["scheduled_at"], "url": r["url"], "calendar_id": r["id"]}
            for r in calendar_rows
        ]

        new_ids = {c.id for c in comparison.new}
        continuing_ids = {c["current"].id for c in comparison.continuing}

        def _status_for(cluster):
            if cluster.id in new_ids:
                return "new"
            if cluster.id in continuing_ids:
                return "continuing"
            return "unchanged"

        trend_dicts = [_trend_dict(conn, cluster, _status_for(cluster)) for cluster in selected_clusters]

        prior_summary = {
            "signal_titles": _titles_for_rows(signal),
            "noise_titles": _titles_for_rows(noise),
            "unresolved_titles": _titles_for_rows(unresolved),
            "resolved_titles": [", ".join(c.key_entities) or c.category for c in comparison.resolved],
        }

        rendered_text = render_monthly(month_label, trend_dicts, prior_summary, calendar_candidates)
        model_used = "none (deterministic fallback)"
        fallback_used = True

        parts = split_monthly(rendered_text, month_label, config.policy.digests.telegram_hard_limit_chars)

        chat_suffix = chat_id_hash(chat_id) if chat_id is not None else ""
        sent = False
        sent_dry_run = True
        idempotency_key = _monthly_idempotency_key(month_label_key, chat_suffix)

        if chat_id is not None:
            max_chars = config.policy.digests.telegram_hard_limit_chars
            if not selected_clusters:
                reason = "no source-backed clusters selected for this month -- refusing to send an empty digest"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return MonthlySummary(
                    idempotency_key=idempotency_key, sent_dry_run=True, sent=False, month_label=month_label,
                    fail_closed=True, fail_reason=reason,
                )
            for i, part in enumerate(parts, start=1):
                if not part.strip():
                    reason = f"part {i}/{len(parts)} is empty"
                elif len(part) > max_chars:
                    reason = f"part {i}/{len(parts)} is {len(part)} chars, exceeds {max_chars}"
                else:
                    continue
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return MonthlySummary(
                    rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                    sent_dry_run=True, sent=False, month_label=month_label, fail_closed=True, fail_reason=reason,
                )

        if chat_id is not None:
            chat_hash = chat_suffix
            existing_delivery = conn.execute(
                "SELECT id, status FROM monthly_deliveries WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_delivery is not None and existing_delivery["status"] in (
                monthly_delivery_repo.STATUS_PARTIAL, monthly_delivery_repo.STATUS_FAILED
            ) and not resume_authorized:
                reason = (
                    f"a prior delivery attempt for {idempotency_key} is {existing_delivery['status']!r} -- "
                    "not automatically retrying; explicit resume_authorized=True is required"
                )
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return MonthlySummary(
                    rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                    sent_dry_run=True, sent=False, month_label=month_label, fail_closed=True, fail_reason=reason,
                )

            delivery_id = monthly_delivery_repo.get_or_create_delivery(
                conn, idempotency_key=idempotency_key, chat_id_hash=chat_hash,
                month_label=month_label, total_parts=len(parts),
            )
            sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
            try:
                any_sent = False
                delivery_failed = False
                for i, part in enumerate(parts, start=1):
                    existing_part = monthly_delivery_repo.get_part(conn, delivery_id, i)
                    if existing_part is not None and existing_part.status in (
                        monthly_delivery_repo.PART_SENT, monthly_delivery_repo.PART_DRY_RUN
                    ):
                        any_sent = any_sent or existing_part.status == monthly_delivery_repo.PART_SENT
                        continue
                    part_key = f"{idempotency_key}:{i}of{len(parts)}"
                    try:
                        result = asyncio.run(
                            sender.send(
                                conn, chat_id=chat_id, text=part, kind="monthly", idempotency_key=part_key,
                                max_chars=config.policy.digests.telegram_hard_limit_chars, dry_run=dry_run,
                            )
                        )
                    except ChatNotAllowedError:
                        raise
                    except Exception:  # noqa: BLE001
                        monthly_delivery_repo.record_part_attempt(
                            conn, delivery_id, i, text=part, status=monthly_delivery_repo.PART_FAILED
                        )
                        delivery_failed = True
                        break
                    part_status = (
                        monthly_delivery_repo.PART_SENT if not result.dry_run
                        else monthly_delivery_repo.PART_DRY_RUN
                    )
                    message_id = ",".join(result.message_ids) if result.message_ids else None
                    monthly_delivery_repo.record_part_attempt(
                        conn, delivery_id, i, text=part, status=part_status, telegram_message_id=message_id
                    )
                    any_sent = any_sent or not result.dry_run
                    sent_dry_run = result.dry_run
                sent = any_sent

                all_parts = monthly_delivery_repo.all_parts(conn, delivery_id)
                if delivery_failed:
                    monthly_delivery_repo.finalize_delivery(conn, delivery_id, monthly_delivery_repo.STATUS_PARTIAL)
                elif all(p.status == monthly_delivery_repo.PART_SENT for p in all_parts):
                    monthly_delivery_repo.finalize_delivery(conn, delivery_id, monthly_delivery_repo.STATUS_SENT)
                elif all(
                    p.status in (monthly_delivery_repo.PART_SENT, monthly_delivery_repo.PART_DRY_RUN)
                    for p in all_parts
                ):
                    monthly_delivery_repo.finalize_delivery(conn, delivery_id, monthly_delivery_repo.STATUS_SENT)

                if delivery_failed:
                    reason = "one or more monthly delivery parts failed to send; logical delivery marked partial"
                    job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                    return MonthlySummary(
                        rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                        sent_dry_run=True, sent=any_sent, month_label=month_label,
                        fail_closed=True, fail_reason=reason,
                    )
            except ChatNotAllowedError:
                sent = False
                sent_dry_run = True

        digest_id = create_digest(
            conn, kind="monthly", window_start=window_start, window_end=window_end,
            rendered_text=rendered_text, model_used=model_used, fallback_used=fallback_used,
        )
        for cluster in selected_clusters:
            section = cluster.category if cluster.category in _SECTION_CATEGORIES else "other"
            add_digest_events(conn, digest_id, cluster.event_ids, section)

        job_runs.finish(
            conn, run_id, "success", {"trend_count": len(selected_clusters), "fallback_used": fallback_used}
        )

        return MonthlySummary(
            rendered_parts=parts, character_count=len(rendered_text), trend_count=len(selected_clusters),
            fallback_used=fallback_used, model_used=model_used, idempotency_key=idempotency_key,
            sent_dry_run=sent_dry_run, sent=sent, month_label=month_label,
        )
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.release_job_lock(conn, "monthly")
