"""Telegram query command handlers (design doc section 4.4, Phase 2). Pure
functions over the DB/config -- no network calls, no running bot process.
Wiring these into a live `python-telegram-bot` long-polling Application
(`pi-news-bot.service`) is left for when the user is ready to test real
Telegram interaction; only allowlisted chat ids may ever reach these in
production."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta

from news_intel.event_repo import event_sources
from news_intel.runtime_state import MUTE_UNTIL_KEY, set_value
from news_intel.settings import AppConfig

_TOPIC_CATEGORY_MAP = {
    "tech": {"AI & Semiconductors", "Technology"},
    "market": {"Markets & Economy"},
    "china": {"US / China / World"},
    "us": {"US / China / World"},
    "world": {"US / China / World"},
}


def cmd_status(conn: sqlite3.Connection, config: AppConfig) -> str:
    from news_intel.sources_repo import health_summary

    rows = health_summary(conn)
    healthy = sum(1 for r in rows if r["health_state"] == "healthy")
    degraded = sum(1 for r in rows if r["health_state"] == "degraded")
    unavailable = sum(1 for r in rows if r["health_state"] in ("unavailable", "disabled"))
    return (
        f"Sources: {healthy} healthy, {degraded} degraded, {unavailable} unavailable/disabled "
        f"(of {len(rows)} configured)."
    )


def cmd_now(conn: sqlite3.Connection, config: AppConfig, *, now: datetime | None = None) -> str:
    """/now — qualified events from the last few hours, most important first."""
    now = now or datetime.now(UTC)
    window_start = now - timedelta(hours=6)
    rows = conn.execute(
        "SELECT id, primary_title, category, final_score FROM events "
        "WHERE final_score >= ? AND last_updated_at >= ? ORDER BY final_score DESC LIMIT 5",
        (config.policy.scoring.thresholds.daily_quick_brief, window_start.isoformat()),
    ).fetchall()
    if not rows:
        return "No qualified events in the last 6 hours."
    lines = ["Recent qualified events:"]
    for r in rows:
        lines.append(f"• [{r['category'] or 'uncategorized'}] {r['primary_title']} ({r['id']})")
    return "\n".join(lines)


def cmd_topic(conn: sqlite3.Connection, config: AppConfig, topic: str, *, now: datetime | None = None) -> str:
    categories = _TOPIC_CATEGORY_MAP.get(topic.lower())
    if not categories:
        return f"Unknown topic {topic!r}. Try: tech, market, china, us, world."
    now = now or datetime.now(UTC)
    window_start = now - timedelta(hours=24)
    placeholders = ",".join("?" for _ in categories)
    rows = conn.execute(
        f"SELECT id, primary_title, category FROM events WHERE category IN ({placeholders}) "
        "AND final_score >= ? AND last_updated_at >= ? ORDER BY final_score DESC LIMIT 5",
        (*categories, config.policy.scoring.thresholds.daily_quick_brief, window_start.isoformat()),
    ).fetchall()
    if not rows:
        return f"No qualified {topic} events in the last 24 hours."
    lines = [f"{topic.capitalize()} — last 24h:"]
    for r in rows:
        lines.append(f"• {r['primary_title']} ({r['id']})")
    return "\n".join(lines)


def cmd_why(conn: sqlite3.Connection, event_id: str) -> str:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        return f"No event found with id {event_id}."
    sources = event_sources(conn, event_id)
    return (
        f"{row['primary_title']}\n"
        f"Category: {row['category'] or 'uncategorized'} | Confirmation: {row['confirmation_state']} | "
        f"Score: {row['final_score']:.0f}\n"
        f"Based on {len(sources)} source(s): {', '.join(s['publisher'] for s in sources) or 'none'}"
    )


def cmd_sources(conn: sqlite3.Connection, event_id: str) -> str:
    sources = event_sources(conn, event_id)
    if not sources:
        return f"No sources recorded for {event_id}."
    lines = [f"Sources for {event_id}:"]
    for s in sources:
        lines.append(f"• [{'T' + str(s['tier'])}] {s['publisher']} — {s['url']}")
    return "\n".join(lines)


_DURATION_RE = re.compile(r"^(\d+)\s*(m|h|min|hr|hour|hours|minute|minutes)$", re.IGNORECASE)


def cmd_mute(conn: sqlite3.Connection, duration_text: str, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    match = _DURATION_RE.match(duration_text.strip())
    if not match:
        return "Usage: /mute <Nm|Nh>, e.g. /mute 2h. S3 critical alerts still interrupt."
    amount, unit = match.groups()
    minutes = int(amount) * (60 if unit.lower().startswith("h") else 1)
    until = now + timedelta(minutes=minutes)
    set_value(conn, MUTE_UNTIL_KEY, until.isoformat(), now=now)
    return f"Muted S2 alerts until {until.isoformat()} (S3 critical alerts still interrupt)."


def cmd_feedback(
    conn: sqlite3.Connection, event_id: str, verdict: str, *, chat_id_hash: str, now: datetime | None = None
) -> str:
    now = now or datetime.now(UTC)
    if verdict not in ("useful", "noise"):
        return "Usage: /feedback E-... useful|noise"
    conn.execute(
        "INSERT INTO feedback (event_id, chat_id_hash, verdict, created_at) VALUES (?, ?, ?, ?)",
        (event_id, chat_id_hash, verdict, now.isoformat()),
    )
    return f"Thanks — recorded {verdict!r} for {event_id}."
