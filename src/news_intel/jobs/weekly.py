"""Phase 3: weekly intelligence brief (design doc sections 3.5, 4.3, 8.4).
Runs Sunday 22:00 America/Los_Angeles, replacing that day's daily send (see
jobs/daily.py's Sunday guard). Window bounds are derived from calendar
arithmetic (the ISO Mon-Sun week containing `now`), not from the exact
instant `now` falls on, so repeated calls within the same week always
compute identical bounds -- that's what makes the digest-row idempotency
check below reliable across independent invocations, not just repeated
calls with one fixed `now` in a test.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from zoneinfo import ZoneInfo

from news_intel import db
from news_intel.delivery import weekly_delivery_repo
from news_intel.delivery.telegram import ChatNotAllowedError, TelegramSender, chat_id_hash
from news_intel.delivery.weekly_formatter import render_weekly, split_weekly
from news_intel.event_repo import add_digest_events, create_digest, events_in_window
from news_intel.jobs import job_runs
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.fallback import fallback_weekly_digest
from news_intel.llm.provider import get_provider
from news_intel.models import WeeklyDigestInput
from news_intel.pipeline.entities import known_entities
from news_intel.pipeline.trends import TrendCluster, build_trend_clusters, select_top_clusters
from news_intel.pipeline.weekly import (
    classify_statement_vs_policy,
    compare_with_prior_week,
    identify_noise_vs_signal,
    unresolved_stories,
)
from news_intel.scheduled_repo import upcoming
from news_intel.settings import AppConfig

_SECTION_CATEGORIES = {"AI & Semiconductors", "Markets & Economy", "US / China / World"}


@dataclass
class WeeklySummary:
    rendered_parts: list[str] = field(default_factory=list)
    character_count: int = 0
    trend_count: int = 0
    fallback_used: bool = False
    model_used: str = ""
    idempotency_key: str = ""
    sent_dry_run: bool = True
    sent: bool = False
    skipped_lock: bool = False
    skipped_duplicate: bool = False
    week_label: str = ""
    fail_closed: bool = False
    fail_reason: str | None = None


def week_window(now_pt: datetime) -> tuple[datetime, datetime]:
    """Stable Mon 00:00 -> next Mon 00:00 bounds for the ISO week containing
    `now_pt`, independent of the exact time of day."""
    tz = now_pt.tzinfo
    monday: date = now_pt.date() - timedelta(days=now_pt.weekday())
    next_monday = monday + timedelta(days=7)
    return (
        datetime.combine(monday, dtime.min, tzinfo=tz),
        datetime.combine(next_monday, dtime.min, tzinfo=tz),
    )


def _titles_for_events(conn: sqlite3.Connection, event_ids: list[str]) -> list[str]:
    if not event_ids:
        return []
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(f"SELECT primary_title FROM events WHERE id IN ({placeholders})", event_ids).fetchall()
    return [r["primary_title"] for r in rows]


def _titles_for_rows(rows: list[sqlite3.Row]) -> list[str]:
    return [r["primary_title"] for r in rows]


def _find_existing_weekly_digest(
    conn: sqlite3.Connection, window_start: datetime, window_end: datetime, *, edition_id: str = "en_global"
):
    return conn.execute(
        "SELECT * FROM digests WHERE kind = 'weekly' AND edition_id = ? AND window_start = ? AND window_end = ? "
        "AND generation_state = 'generated' ORDER BY created_at DESC LIMIT 1",
        (edition_id, window_start.isoformat(), window_end.isoformat()),
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


def _weekly_idempotency_key(edition_id: str, week_label: str, chat_suffix: str) -> str:
    """2026-08-17 multi-edition architecture: en_global keeps its
    PRE-EXISTING key format (no edition segment) so already-created dates'
    idempotency behavior is unchanged -- a new zh_cn key includes the
    edition explicitly (weekly:zh_cn:<ISO-week>:<hash>) so an English and a
    Chinese delivery can never collide merely because they share a
    bot/recipient, per this round's explicit instruction."""
    if edition_id == "en_global":
        return f"weekly:{week_label}:{chat_suffix or 'no-chat'}"
    return f"weekly:{edition_id}:{week_label}:{chat_suffix or 'no-chat'}"


def run_weekly(
    config: AppConfig,
    conn: sqlite3.Connection,
    *,
    chat_id: int | None = None,
    dry_run: bool = True,
    now: datetime | None = None,
    resume_authorized: bool = False,
    edition_id: str = "en_global",
) -> WeeklySummary:
    now = now or datetime.now(UTC)
    edition = config.editions.get(edition_id)
    tz = ZoneInfo(edition.schedule.timezone)
    now_pt = now.astimezone(tz)
    event_topics = config.topics_for(edition_id)

    # 2026-08-17: edition-scoped job lock -- a zh_cn weekly run must never
    # be blocked by (or block) an en_global weekly run, and vice versa;
    # "a failure in zh_cn must not abort or corrupt en_global."
    lock_name = "weekly" if edition_id == "en_global" else f"weekly_{edition_id}"
    if not db.acquire_job_lock(conn, lock_name):
        return WeeklySummary(skipped_lock=True)

    run_id = job_runs.start(conn, "weekly")
    try:
        window_start, window_end = week_window(now_pt)
        prior_start, prior_end = window_start - timedelta(days=7), window_start

        iso_year, iso_week, _ = now_pt.isocalendar()
        week_label = f"{iso_year}-W{iso_week:02d}"
        date_label = f"{window_start.strftime('%b %-d')}–{(window_end - timedelta(days=1)).strftime('%b %-d')}"

        existing = _find_existing_weekly_digest(conn, window_start, window_end, edition_id=edition_id)
        if existing is not None:
            parts = split_weekly(
                existing["rendered_text"], date_label, config.policy.digests.telegram_hard_limit_chars,
                edition_id=edition_id,
            )
            return WeeklySummary(
                rendered_parts=parts, character_count=len(existing["rendered_text"]),
                model_used=existing["model_used"], fallback_used=bool(existing["fallback_used"]),
                idempotency_key=_weekly_idempotency_key(edition_id, week_label, "no-chat"),
                sent_dry_run=True, sent=False,
                skipped_duplicate=True, week_label=week_label,
            )

        known = known_entities(event_topics.groups)
        current_events = events_in_window(
            conn, window_start=window_start, window_end=window_end, edition_id=edition_id
        )
        prior_events = events_in_window(
            conn, window_start=prior_start, window_end=prior_end, edition_id=edition_id
        )

        trend_policy = event_topics.trend_identity
        min_event_score = config.policy.scoring.thresholds.weekly_store
        # Comparison (new/continuing/resolved) runs against the *full*
        # eligible cluster set so a genuinely continuing trend is never
        # missed just because it later gets trimmed from the brief itself.
        current_clusters = build_trend_clusters(
            conn, current_events, known, policy=trend_policy, min_event_score=min_event_score
        )
        prior_clusters = build_trend_clusters(
            conn, prior_events, known, policy=trend_policy, min_event_score=min_event_score
        )
        comparison = compare_with_prior_week(current_clusters, prior_clusters)
        # Gate A remediation Phase 4: only a bounded, config-driven set of
        # the highest-quality clusters actually goes to the composer --
        # Gate A produced 915 low-value clusters for one week alone.
        selected_clusters = select_top_clusters(
            current_clusters, max_items=config.policy.digests.weekly_max_selected_items
        )

        thresholds = config.policy.scoring.thresholds
        noise, signal = identify_noise_vs_signal(
            current_events, signal_threshold=thresholds.daily_top3, noise_threshold=thresholds.weekly_store
        )
        unresolved = unresolved_stories(current_events)
        became_policy, remained_statement = classify_statement_vs_policy(conn, current_events, config.alert_triggers)

        calendar_rows = upcoming(conn, within_hours=24 * 7, now=now, edition_id=edition_id)
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
            "became_policy_titles": _titles_for_rows(became_policy),
            "remained_statement_titles": _titles_for_rows(remained_statement),
        }

        digest_input = WeeklyDigestInput(
            week_label=date_label, events=trend_dicts, prior_week_summary=prior_summary,
            calendar_candidates=calendar_candidates,
        )

        provider = get_provider(config)
        model_used = "none (deterministic fallback)"
        fallback_used = False
        try:
            output = provider.compose_weekly(digest_input)
            rendered_text = render_weekly(date_label, output.structured, edition_id=edition_id)
            model_used = getattr(provider, "model", "llm")
        except LLMUnavailableError:
            fallback_output = fallback_weekly_digest(digest_input, date_label, edition_id=edition_id)
            rendered_text = fallback_output.rendered_text
            fallback_used = True

        parts = split_weekly(
            rendered_text, date_label, config.policy.digests.telegram_hard_limit_chars, edition_id=edition_id
        )

        chat_suffix = chat_id_hash(chat_id) if chat_id is not None else ""
        sent = False
        sent_dry_run = True
        idempotency_key = _weekly_idempotency_key(edition_id, week_label, chat_suffix)

        # 2026-08-17: fail closed on content/provenance BEFORE ever
        # contacting Telegram (mirrors jobs/daily.py's production
        # hardening) -- every part must be non-empty, real content, and
        # within the hard Telegram limit (defense in depth on top of
        # split_weekly's own targeting), and there must be at least one
        # substantiated event/source-backed cluster behind the digest.
        if chat_id is not None:
            max_chars = config.policy.digests.telegram_hard_limit_chars
            if not selected_clusters:
                reason = "no source-backed clusters selected for this week -- refusing to send an empty digest"
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return WeeklySummary(
                    idempotency_key=idempotency_key, sent_dry_run=True, sent=False, week_label=week_label,
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
                return WeeklySummary(
                    rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                    sent_dry_run=True, sent=False, week_label=week_label, fail_closed=True, fail_reason=reason,
                )

        if chat_id is not None:
            chat_hash = chat_suffix
            # Safe multipart delivery (2026-08-17): a PRIOR partial/failed
            # logical delivery for this exact idempotency_key is never
            # auto-resumed -- resuming requires the caller to explicitly
            # pass resume_authorized=True (a separate, deliberate action,
            # never triggered by an ordinary re-invocation of this job).
            existing_delivery = conn.execute(
                "SELECT id, status FROM weekly_deliveries WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
            if existing_delivery is not None and existing_delivery["status"] in (
                weekly_delivery_repo.STATUS_PARTIAL, weekly_delivery_repo.STATUS_FAILED
            ) and not resume_authorized:
                reason = (
                    f"a prior delivery attempt for {idempotency_key} is {existing_delivery['status']!r} -- "
                    "not automatically retrying; explicit resume_authorized=True is required"
                )
                job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                return WeeklySummary(
                    rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                    sent_dry_run=True, sent=False, week_label=week_label, fail_closed=True, fail_reason=reason,
                )

            delivery_id = weekly_delivery_repo.get_or_create_delivery(
                conn, idempotency_key=idempotency_key, chat_id_hash=chat_hash,
                period_label=week_label, total_parts=len(parts), edition_id=edition_id, kind="weekly",
            )
            sender = TelegramSender(config.secrets.telegram_bot_token, config.secrets.allowed_chat_id_list)
            try:
                any_sent = False
                delivery_failed = False
                for i, part in enumerate(parts, start=1):
                    existing_part = weekly_delivery_repo.get_part(conn, delivery_id, i)
                    if existing_part is not None and existing_part.status in (
                        weekly_delivery_repo.PART_SENT, weekly_delivery_repo.PART_DRY_RUN
                    ):
                        # Never resend a successfully (or dry-run) sent part.
                        any_sent = any_sent or existing_part.status == weekly_delivery_repo.PART_SENT
                        continue
                    part_key = f"{idempotency_key}:{i}of{len(parts)}"
                    try:
                        result = asyncio.run(
                            sender.send(
                                conn, chat_id=chat_id, text=part, kind="weekly", idempotency_key=part_key,
                                max_chars=config.policy.digests.telegram_hard_limit_chars, dry_run=dry_run,
                            )
                        )
                    except ChatNotAllowedError:
                        raise
                    except Exception:  # noqa: BLE001
                        # A part-level send failure (e.g. a genuine Telegram
                        # API error) marks the logical delivery partial/
                        # failed and stops -- no automatic retry of this or
                        # any later part from within this invocation.
                        weekly_delivery_repo.record_part_attempt(
                            conn, delivery_id, i, text=part, status=weekly_delivery_repo.PART_FAILED
                        )
                        delivery_failed = True
                        break
                    part_status = (
                        weekly_delivery_repo.PART_SENT if not result.dry_run else weekly_delivery_repo.PART_DRY_RUN
                    )
                    message_id = ",".join(result.message_ids) if result.message_ids else None
                    weekly_delivery_repo.record_part_attempt(
                        conn, delivery_id, i, text=part, status=part_status, telegram_message_id=message_id
                    )
                    any_sent = any_sent or not result.dry_run
                    sent_dry_run = result.dry_run
                sent = any_sent

                all_parts = weekly_delivery_repo.all_parts(conn, delivery_id)
                if delivery_failed:
                    weekly_delivery_repo.finalize_delivery(conn, delivery_id, weekly_delivery_repo.STATUS_PARTIAL)
                elif all(p.status == weekly_delivery_repo.PART_SENT for p in all_parts):
                    weekly_delivery_repo.finalize_delivery(conn, delivery_id, weekly_delivery_repo.STATUS_SENT)
                elif all(
                    p.status in (weekly_delivery_repo.PART_SENT, weekly_delivery_repo.PART_DRY_RUN)
                    for p in all_parts
                ):
                    weekly_delivery_repo.finalize_delivery(conn, delivery_id, weekly_delivery_repo.STATUS_SENT)

                if delivery_failed:
                    reason = "one or more weekly delivery parts failed to send; logical delivery marked partial"
                    job_runs.finish(conn, run_id, "failed", {"fail_closed_reason": reason})
                    return WeeklySummary(
                        rendered_parts=parts, character_count=len(rendered_text), idempotency_key=idempotency_key,
                        sent_dry_run=True, sent=any_sent, week_label=week_label,
                        fail_closed=True, fail_reason=reason,
                    )
            except ChatNotAllowedError:
                sent = False
                sent_dry_run = True

        digest_id = create_digest(
            conn, kind="weekly", window_start=window_start, window_end=window_end,
            rendered_text=rendered_text, model_used=model_used, fallback_used=fallback_used,
            edition_id=edition_id,
        )
        for cluster in selected_clusters:
            section = cluster.category if cluster.category in _SECTION_CATEGORIES else "other"
            add_digest_events(conn, digest_id, cluster.event_ids, section)

        job_runs.finish(
            conn, run_id, "success", {"trend_count": len(selected_clusters), "fallback_used": fallback_used}
        )

        return WeeklySummary(
            rendered_parts=parts, character_count=len(rendered_text), trend_count=len(selected_clusters),
            fallback_used=fallback_used, model_used=model_used, idempotency_key=idempotency_key,
            sent_dry_run=sent_dry_run, sent=sent, week_label=week_label,
        )
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise
    finally:
        db.release_job_lock(conn, lock_name)
