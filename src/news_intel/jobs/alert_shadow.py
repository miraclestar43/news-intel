"""Breaking-alert shadow canary.

Runs the real, unmodified alert decision engine (jobs/alerts.py:
process_event_for_alerts / run_alert_check / finalize_expired_merge_batches
/ release_held_alerts) against live events and persists every candidate,
decision, trigger reason, severity, event/source mapping, suppression
reason, and a hypothetical rendered message to alert_shadow_log -- without
ever calling Telegram or writing a genuine delivery record.

Deliberately does NOT import news_intel.delivery.telegram at all (not just
"doesn't call send()") so the absence of any real dispatch is an import-
time fact, not a runtime one. would_have_sent is derived entirely from
decisions the existing engine already made (action == "sent", a finalized
merge batch, or a released overnight batch) -- this module adds observation
and persistence only, never new eligibility/cooldown/budget/spacing logic.

Re-running against the same unchanged state (e.g. a "still suppressed_
cooldown" or "still pending_merge" result on every 5-minute tick, since
run_alert_check re-evaluates every non-resolved event every time) does not
spam alert_shadow_log: a new row is written only when the observed
(action, reason, message_hash) for that event_id actually changed since the
last shadow observation -- see _log_if_changed."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from news_intel import alert_shadow_repo as shadow
from news_intel.delivery.alert_formatter import render_alert
from news_intel.event_repo import event_sources
from news_intel.jobs import job_runs
from news_intel.jobs.alerts import finalize_expired_merge_batches, release_held_alerts, run_alert_check
from news_intel.models import ConfirmationState, Severity
from news_intel.settings import AppConfig


def _source_urls(conn: sqlite3.Connection, event_id: str) -> list[dict]:
    return [{"publisher": s["publisher"], "url": s["url"]} for s in event_sources(conn, event_id)]


def _trigger_id_from_reason(reason: str | None) -> str | None:
    if reason and reason.startswith("trigger="):
        return reason.removeprefix("trigger=")
    return None


def _hypothetical_render(
    conn: sqlite3.Connection, event_row: sqlite3.Row, *, severity: str, trigger_id: str | None,
    is_statement_only: bool,
) -> str:
    """Deterministic preview of what a solo alert message would look like --
    no LLM call (english_headline/what_happened come straight from the
    stored title/excerpt; chinese_headline/chinese_takeaway are honestly
    left blank rather than fabricated), mirroring the project's established
    deterministic-fallback pattern (llm/fallback.py)."""
    sources = event_sources(conn, event_row["id"])
    excerpt = sources[0]["excerpt"] if sources else ""
    return render_alert(
        event_id=event_row["id"],
        category=event_row["category"] or "uncategorized",
        english_headline=event_row["primary_title"] or "",
        chinese_headline="",
        what_happened=(excerpt or "")[:280],
        why_it_matters=f"Matched deterministic trigger {trigger_id!r}." if trigger_id else "",
        chinese_takeaway="",
        confirmation_state=ConfirmationState(event_row["confirmation_state"]),
        severity=Severity(severity),
        is_statement_only=is_statement_only,
        sources=[s["publisher"] for s in sources],
    )


def _log_if_changed(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    run_kind: str,
    event_id: str,
    primary_title: str | None,
    category: str | None,
    severity: str | None,
    trigger_id: str | None,
    is_statement_only: bool,
    action: str,
    reason: str | None,
    message_hash: str | None,
    eligible_at: datetime | None,
    would_have_sent: bool,
    rendered_text: str | None,
) -> bool:
    last = shadow.last_shadow_row(conn, event_id)
    if (
        last is not None
        and last["action"] == action
        and (last["message_hash"] or None) == (message_hash or None)
        and (last["reason"] or None) == (reason or None)
    ):
        return False
    shadow.record_shadow_observation(
        conn,
        evaluated_at=now,
        run_kind=run_kind,
        event_id=event_id,
        primary_title=primary_title,
        category=category,
        severity=severity,
        trigger_id=trigger_id,
        is_statement_only=is_statement_only,
        action=action,
        reason=reason,
        message_hash=message_hash,
        eligible_at=eligible_at,
        would_have_sent=would_have_sent,
        hypothetical_rendered_text=rendered_text,
        source_urls_json=json.dumps(_source_urls(conn, event_id), ensure_ascii=False),
        now=now,
    )
    return True


def _latest_alert_queue_row(conn: sqlite3.Connection, event_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM alert_queue WHERE event_id = ? ORDER BY id DESC LIMIT 1", (event_id,)
    ).fetchone()


def run_alert_shadow_check(
    config: AppConfig, conn: sqlite3.Connection, *, now: datetime | None = None
) -> dict:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    run_id = job_runs.start(conn, "alert_shadow")
    logged = 0
    evaluated = 0
    try:
        # 1. Finalize any due S2 burst-merge batches first (existing,
        # unmodified logic), so a batch that closes between ticks gets its
        # true would-send verdict (solo vs merged) captured with the exact
        # rendered text the real engine already produced -- before
        # run_alert_check's own internal finalize call (a harmless no-op by
        # then) and fresh per-event evaluation run below.
        finalized = finalize_expired_merge_batches(config, conn, now)
        for batch in finalized:
            for event_id in batch["event_ids"]:
                aq_row = conn.execute(
                    "SELECT * FROM alert_queue WHERE event_id = ? AND message_hash = ? AND state = 'sent' "
                    "ORDER BY id DESC LIMIT 1",
                    (event_id, batch["message_hash"]),
                ).fetchone()
                event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
                if _log_if_changed(
                    conn, now=now, run_kind="merge_finalize", event_id=event_id,
                    primary_title=event_row["primary_title"] if event_row else None,
                    category=event_row["category"] if event_row else None,
                    severity="S2", trigger_id=_trigger_id_from_reason(aq_row["reason"] if aq_row else None),
                    is_statement_only=False, action="sent",
                    reason=aq_row["reason"] if aq_row else "S2 burst-merge window finalized",
                    message_hash=batch["message_hash"], eligible_at=None, would_have_sent=True,
                    rendered_text=batch["rendered_text"],
                ):
                    logged += 1

        # 2. The real decision engine, unmodified, over every non-resolved event.
        results = run_alert_check(config, conn, now=now)
        evaluated = len(results)
        for result in results:
            event_row = conn.execute("SELECT * FROM events WHERE id = ?", (result.event_id,)).fetchone()
            if event_row is None:
                continue

            if result.action == "not_eligible":
                reason = result.reason
                message_hash = None
                eligible_at = None
            else:
                aq_row = _latest_alert_queue_row(conn, result.event_id)
                reason = aq_row["reason"] if aq_row else result.reason
                message_hash = result.message_hash or (aq_row["message_hash"] if aq_row else None)
                eligible_at = None
                if aq_row is not None and aq_row["eligible_at"]:
                    eligible_at = datetime.fromisoformat(aq_row["eligible_at"])

            would_have_sent = result.action == "sent"
            rendered_text = None
            if result.severity and result.action in ("sent", "pending_merge"):
                rendered_text = _hypothetical_render(
                    conn, event_row, severity=result.severity,
                    trigger_id=_trigger_id_from_reason(reason), is_statement_only=result.is_statement_only,
                )

            if _log_if_changed(
                conn, now=now, run_kind="evaluate", event_id=result.event_id,
                primary_title=event_row["primary_title"], category=event_row["category"],
                severity=result.severity, trigger_id=_trigger_id_from_reason(reason),
                is_statement_only=result.is_statement_only, action=result.action, reason=reason,
                message_hash=message_hash, eligible_at=eligible_at, would_have_sent=would_have_sent,
                rendered_text=rendered_text,
            ):
                logged += 1

        # 3. The consolidated overnight release path (existing, unmodified
        # logic): safe to call every tick since held_alerts_due() already
        # filters to eligible_at <= now, so this only does something on the
        # tick(s) that actually cross the quiet-hours-end boundary.
        release = release_held_alerts(config, conn, now=now)
        for event_id in release.released_event_ids:
            event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if _log_if_changed(
                conn, now=now, run_kind="overnight_release", event_id=event_id,
                primary_title=event_row["primary_title"] if event_row else None,
                category=event_row["category"] if event_row else None,
                severity="S2", trigger_id=None, is_statement_only=False, action="sent",
                reason="released at overnight consolidated release", message_hash=None,
                eligible_at=None, would_have_sent=True, rendered_text=release.rendered_text,
            ):
                logged += 1
        for event_id in release.dropped_event_ids:
            event_row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
            if _log_if_changed(
                conn, now=now, run_kind="overnight_release", event_id=event_id,
                primary_title=event_row["primary_title"] if event_row else None,
                category=event_row["category"] if event_row else None,
                severity="S2", trigger_id=None, is_statement_only=False, action="dropped_overnight",
                reason="disputed or no longer eligible at release time", message_hash=None,
                eligible_at=None, would_have_sent=False, rendered_text=None,
            ):
                logged += 1

        job_runs.finish(conn, run_id, "success", {"evaluated": evaluated, "shadow_rows_logged": logged})
    except Exception as exc:  # noqa: BLE001
        job_runs.finish(conn, run_id, "failed", {"error": str(exc)})
        raise

    return {"evaluated": evaluated, "shadow_rows_logged": logged}
