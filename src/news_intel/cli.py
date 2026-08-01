from __future__ import annotations

import typer

from news_intel import db
from news_intel.jobs import job_runs
from news_intel.jobs.collect import run_collect
from news_intel.jobs.daily import run_daily
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
        typer.echo(f"  {s.source_id:<24} {s.health_state:<12} items={s.item_count} {s.detail or ''}")
    typer.echo(
        f"new_raw_items={summary.new_raw_items} new_events={summary.new_events} "
        f"updated_events={summary.updated_events}"
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


if __name__ == "__main__":
    app()
