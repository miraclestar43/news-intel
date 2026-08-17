from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import typer

from news_intel import db
from news_intel.jobs import job_runs
from news_intel.jobs.alert_shadow import run_alert_shadow_check
from news_intel.jobs.alerts import release_held_alerts, run_alert_check
from news_intel.jobs.collect import run_collect
from news_intel.jobs.daily import run_daily
from news_intel.jobs.daily_canary import run_daily_canary
from news_intel.jobs.weekly import run_weekly
from news_intel.jobs.weekly_production import run_weekly_production
from news_intel.logging_setup import configure_logging, get_logger
from news_intel.settings import (
    DEFAULT_ENV_FILE,
    env_file_permission_warning,
    load_config,
    reset_config_cache,
)
from news_intel.sources_repo import health_summary

app = typer.Typer(add_completion=False, help="pi-news-intel: low-noise personal news intelligence")
log = get_logger(__name__)


def _load() -> tuple:
    reset_config_cache()
    config = load_config()
    configure_logging(log_dir=config.data_dir / "logs")
    conn = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(conn)
    return config, conn


@app.command()
def doctor() -> None:
    """Environment/config readiness check. Never prints secret values."""
    problems: list[str] = []
    warnings: list[str] = []

    try:
        config, conn = _load()
    except Exception as exc:  # noqa: BLE001
        typer.echo(f"FAIL: config/migration error: {exc}")
        raise typer.Exit(code=1) from exc

    typer.echo(f"OK: config loaded from {config.config_dir}")
    typer.echo(f"OK: data dir {config.data_dir}")
    typer.echo(f"OK: {len(config.sources.sources)} sources configured")

    perm_warning = env_file_permission_warning()
    if perm_warning:
        warnings.append(perm_warning)
    elif not DEFAULT_ENV_FILE.exists():
        warnings.append(
            f"secrets file not found at {DEFAULT_ENV_FILE} — Telegram/LLM secrets unset (ok for now)"
        )
    else:
        typer.echo(f"OK: secrets file present with safe permissions ({DEFAULT_ENV_FILE})")

    if not config.secrets.telegram_bot_token:
        warnings.append("TELEGRAM_BOT_TOKEN not set — sends will stay in dry-run")
    if not (config.secrets.llm_base_url and config.secrets.llm_api_key and config.secrets.llm_model):
        warnings.append("LLM_BASE_URL/LLM_API_KEY/LLM_MODEL not fully set — deterministic fallback will be used")

    for w in warnings:
        typer.echo(f"WARN: {w}")
    for p in problems:
        typer.echo(f"FAIL: {p}")

    conn.close()
    if problems:
        raise typer.Exit(code=1)
    typer.echo("doctor: OK" + (" (with warnings)" if warnings else ""))


@app.command()
def status() -> None:
    """Source health, last collection, last MiMo success, queue counts, last digest/alert."""
    config, conn = _load()

    typer.echo("=== Source health ===")
    for row in health_summary(conn):
        typer.echo(
            f"  {row['id']:<24} tier={row['tier']} enabled={bool(row['enabled'])} "
            f"state={row['health_state']:<12} failures={row['consecutive_failures']} "
            f"last_success={row['last_success_at'] or 'never'}"
        )

    typer.echo("\n=== Job runs (most recent) ===")
    for job_name in ("collect", "daily", "weekly", "held", "cleanup", "health"):
        row = job_runs.last_run(conn, job_name)
        if row:
            typer.echo(f"  {job_name:<10} {row['status']:<8} started={row['started_at']} finished={row['finished_at']}")
        else:
            typer.echo(f"  {job_name:<10} never run")

    counts = conn.execute(
        "SELECT (SELECT COUNT(*) FROM raw_items) AS raw_items, "
        "(SELECT COUNT(*) FROM events WHERE datetime(first_seen_at) >= datetime('now','-1 day')) AS events_24h, "
        "(SELECT COUNT(*) FROM alert_queue WHERE state='pending') AS pending_alerts, "
        "(SELECT COUNT(*) FROM alert_queue WHERE state='held') AS held_alerts"
    ).fetchone()
    typer.echo("\n=== Counts ===")
    typer.echo(
        f"  raw_items={counts['raw_items']} events_last_24h={counts['events_24h']} "
        f"pending_alerts={counts['pending_alerts']} held_alerts={counts['held_alerts']}"
    )

    db_path = config.data_dir / "news-intel.db"
    size = db_path.stat().st_size if db_path.exists() else 0
    typer.echo(f"\n=== Storage ===\n  db_size_bytes={size}")

    conn.close()


@app.command()
def collect(dry_run: bool = typer.Option(True, help="Preview only; roll back all DB writes at the end.")) -> None:
    """Run one collection pass across all enabled sources."""
    config, conn = _load()
    try:
        if dry_run:
            with db.rollback_only(conn):
                summary = run_collect(config, conn)
        else:
            summary = run_collect(config, conn)
    finally:
        conn.close()

    typer.echo(f"dry_run={dry_run}")
    for s in summary.sources:
        typer.echo(
            f"  {s.source_id:<24} {s.action:<15} {s.health_state:<12} items={s.item_count} {s.detail or ''}"
        )
    typer.echo(
        f"fetched_items={summary.fetched_items} new_raw_items={summary.new_raw_items} "
        f"changed_raw_items={summary.changed_raw_items} unchanged_raw_items={summary.unchanged_raw_items}"
    )
    typer.echo(
        f"new_events={summary.new_events} updated_events={summary.updated_events} "
        f"processed_items={summary.processed_items}"
    )


digest_app = typer.Typer(help="Digest generation commands")
app.add_typer(digest_app, name="digest")


@digest_app.command("daily")
def digest_daily(
    dry_run: bool = typer.Option(True, help="Preview only; do not persist the digest or send Telegram."),
    chat_id: int = typer.Option(None, help="Telegram chat id to send to (must be in the allowlist)."),
) -> None:
    """Generate (and optionally send) the daily brief."""
    config, conn = _load()
    try:
        if dry_run:
            with db.rollback_only(conn):
                summary = run_daily(config, conn, chat_id=chat_id, dry_run=True)
        else:
            summary = run_daily(config, conn, chat_id=chat_id, dry_run=False)
    finally:
        conn.close()

    typer.echo(f"model_used={summary.model_used} fallback_used={summary.fallback_used}")
    typer.echo(f"top3={summary.top3_count} quick_briefs={summary.quick_brief_count} chars={summary.character_count}")
    typer.echo(f"idempotency_key={summary.idempotency_key} sent={summary.sent} sent_dry_run={summary.sent_dry_run}")
    typer.echo("\n--- rendered brief ---\n")
    typer.echo(summary.rendered_text)


@digest_app.command("weekly")
def digest_weekly(
    dry_run: bool = typer.Option(True, help="Preview only; do not persist the digest or send Telegram."),
    chat_id: int = typer.Option(None, help="Telegram chat id to send to (must be in the allowlist)."),
) -> None:
    """Generate (and optionally send) the Sunday weekly brief."""
    config, conn = _load()
    try:
        if dry_run:
            with db.rollback_only(conn):
                summary = run_weekly(config, conn, chat_id=chat_id, dry_run=True)
        else:
            summary = run_weekly(config, conn, chat_id=chat_id, dry_run=False)
    finally:
        conn.close()

    typer.echo(f"week={summary.week_label} model_used={summary.model_used} fallback_used={summary.fallback_used}")
    typer.echo(
        f"trends={summary.trend_count} chars={summary.character_count} "
        f"skipped_duplicate={summary.skipped_duplicate}"
    )
    typer.echo(f"idempotency_key={summary.idempotency_key} sent={summary.sent} sent_dry_run={summary.sent_dry_run}")
    for i, part in enumerate(summary.rendered_parts, start=1):
        typer.echo(f"\n--- rendered part {i}/{len(summary.rendered_parts)} ---\n")
        typer.echo(part)


alerts_app = typer.Typer(help="Breaking-alert eligibility, queueing, and held-alert release")
app.add_typer(alerts_app, name="alerts")


@alerts_app.command("check")
def alerts_check(
    dry_run: bool = typer.Option(True, help="Preview only; roll back all DB writes at the end."),
) -> None:
    """Evaluate every developing event against the alert gate; real Telegram
    sending is never triggered from this command in Phase 2."""
    config, conn = _load()
    try:
        if dry_run:
            with db.rollback_only(conn):
                results = run_alert_check(config, conn)
        else:
            results = run_alert_check(config, conn)
    finally:
        conn.close()

    typer.echo(f"dry_run={dry_run} evaluated={len(results)}")
    for r in results:
        if r.action != "not_eligible":
            typer.echo(f"  {r.event_id} action={r.action} severity={r.severity} reason={r.reason}")


@alerts_app.command("release-held")
def alerts_release_held(
    dry_run: bool = typer.Option(True, help="Preview only; roll back all DB writes at the end."),
) -> None:
    """06:30 America/Los_Angeles consolidated release of held S2 alerts.
    Real Telegram sending is never triggered from this command in Phase 2."""
    config, conn = _load()
    try:
        if dry_run:
            with db.rollback_only(conn):
                result = release_held_alerts(config, conn)
        else:
            result = release_held_alerts(config, conn)
    finally:
        conn.close()

    typer.echo(f"dry_run={dry_run} released={result.released_event_ids} dropped={result.dropped_event_ids}")
    if result.rendered_text:
        typer.echo("\n--- rendered overnight update ---\n")
        typer.echo(result.rendered_text)


@alerts_app.command("shadow-check")
def alerts_shadow_check() -> None:
    """Breaking-alert shadow canary: runs the real alert decision engine
    (jobs/alerts.py, unmodified) and persists every candidate/decision/
    hypothetical rendered message to alert_shadow_log. Never calls Telegram
    and never writes to the deliveries table -- see jobs/alert_shadow.py."""
    config, conn = _load()
    try:
        summary = run_alert_shadow_check(config, conn)
    finally:
        conn.close()

    typer.echo(f"evaluated={summary['evaluated']} shadow_rows_logged={summary['shadow_rows_logged']}")


bot_app = typer.Typer(
    help="Telegram query-command dispatch (one-shot; never starts long-polling)"
)
app.add_typer(bot_app, name="bot")


@bot_app.command("dispatch")
def bot_dispatch(
    update_json: str = typer.Argument(
        ..., help="Path to a JSON file containing one synthetic Telegram Update payload"
    ),
) -> None:
    """Parses a single synthetic Update payload and prints the dispatched
    response. This does NOT start python-telegram-bot's long-polling
    Application -- pi-news-bot.service remains uninstalled/disabled. Useful
    for manually exercising /now, /why, /sources, /mute, etc. without a real
    bot token or network access."""
    import json
    from pathlib import Path

    from telegram import Update

    from news_intel.delivery.bot import handle_update

    config, conn = _load()
    try:
        payload = json.loads(Path(update_json).read_text())
        update = Update.de_json(payload, bot=None)
        response = handle_update(conn, config, update)
    finally:
        conn.close()

    typer.echo(response or "(no response: chat not allowlisted, or unrecognized command)")


canary_app = typer.Typer(help="Bounded three-delivery daily canary (see jobs/daily_canary.py)")
app.add_typer(canary_app, name="canary")


@canary_app.command("daily")
def canary_daily(
    start_date: str = typer.Option(
        ..., envvar="CANARY_START_DATE",
        help="First intended Pacific delivery date, YYYY-MM-DD. Today's Pacific date minus this "
        "date (+1) is the canary_index; invocations outside [1, 3] are skipped as a safety net.",
    ),
) -> None:
    """Real Telegram send is never gated behind --dry-run here: this command
    exists only to run the already-scoped, already-authorized bounded
    canary, which performs its own fail-closed checks (allowlist, event
    eligibility, source-URL mapping, message size, per-date idempotency)
    before ever contacting Telegram -- see run_daily_canary's docstring."""
    config, conn = _load()
    try:
        now = datetime.now(UTC)
        pt_today = now.astimezone(ZoneInfo(config.policy.timezone)).date()
        start = date.fromisoformat(start_date)
        canary_index = (pt_today - start).days + 1
        if not (1 <= canary_index <= 3):
            typer.echo(
                f"canary: pt_date={pt_today} (start={start}) yields canary_index={canary_index}, "
                "outside the bounded [1, 3] window -- skipping, no Telegram contact."
            )
            return
        summary = run_daily_canary(config, conn, canary_index=canary_index, now=now)
    finally:
        conn.close()

    typer.echo(
        f"canary_index={summary.canary_index}/3 pt_date={summary.pt_date} "
        f"fail_closed={summary.fail_closed} sent={summary.sent} "
        f"events={summary.event_count} chars={summary.character_count}"
    )
    if summary.fail_closed:
        typer.echo(f"fail_reason={summary.fail_reason}")
    typer.echo(f"idempotency_key={summary.idempotency_key} message_ids={summary.message_ids}")


production_app = typer.Typer(help="Normal long-term production delivery (promoted from the daily canary)")
app.add_typer(production_app, name="production")


@production_app.command("daily")
def production_daily(
    start_date: str = typer.Option(
        ..., envvar="PRODUCTION_START_DATE",
        help="First Pacific date eligible for a real production send, YYYY-MM-DD. Any invocation on an "
        "earlier Pacific date is skipped as a safety net -- no Telegram contact.",
    ),
) -> None:
    """The production daily entrypoint: always a real (non-dry-run) send.
    Fail-closed checks (allowlist cardinality, event category/URL mapping,
    message length, per-Pacific-date idempotency) all happen inside
    run_daily() before any Telegram contact -- see jobs/daily.py."""
    config, conn = _load()
    try:
        now = datetime.now(UTC)
        pt_today = now.astimezone(ZoneInfo(config.policy.timezone)).date()
        start = date.fromisoformat(start_date)
        if pt_today < start:
            typer.echo(
                f"production: pt_date={pt_today} is before start_date={start} -- skipping, no Telegram contact."
            )
            return

        allowed = config.secrets.allowed_chat_id_list
        chat_id = allowed[0] if len(allowed) == 1 else -1
        summary = run_daily(config, conn, chat_id=chat_id, dry_run=False, now=now)
    finally:
        conn.close()

    typer.echo(
        f"fail_closed={summary.fail_closed} sent={summary.sent} "
        f"top3={summary.top3_count} quick_briefs={summary.quick_brief_count} chars={summary.character_count}"
    )
    if summary.fail_closed:
        typer.echo(f"fail_reason={summary.fail_reason}")
    typer.echo(f"idempotency_key={summary.idempotency_key}")


@production_app.command("weekly")
def production_weekly() -> None:
    """The production weekly entrypoint: always a real (non-dry-run) send,
    Sunday-slot only. Rejects execution outside an explicitly valid Sunday
    America/Los_Angeles window BEFORE any job-run/digest/delivery record is
    created -- this is what keeps a `Persistent=true` timer, installed
    after a missed Sunday slot, from firing a late catch-up send; see
    jobs/weekly_production.py. Never use `pi-news digest weekly` (the
    lower-level dry-run-capable command) as the production service."""
    config, conn = _load()
    try:
        summary = run_weekly_production(config, conn)
    finally:
        conn.close()

    if summary.skipped_late_catchup:
        typer.echo(
            f"production weekly: outside the valid Sunday America/Los_Angeles window at "
            f"{summary.pt_window_checked_at} -- skipped_late_catchup, zero database writes, "
            f"zero Telegram contact."
        )
        return

    typer.echo(
        f"week={summary.week_label} git_head={summary.git_head} fail_closed={summary.fail_closed} "
        f"sent={summary.sent} trends={summary.trend_count} chars={summary.character_count} "
        f"skipped_duplicate={summary.skipped_duplicate}"
    )
    if summary.fail_closed:
        typer.echo(f"fail_reason={summary.fail_reason}")
    typer.echo(f"idempotency_key={summary.idempotency_key}")


if __name__ == "__main__":
    app()
