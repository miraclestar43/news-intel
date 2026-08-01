"""Phase 2 completion item 3: wires the pure command functions in
delivery/commands.py to a dispatcher that understands real
`telegram.Update` payloads, without starting python-telegram-bot's
long-polling Application. `pi-news-bot.service` stays uninstalled/disabled;
this module is only ever invoked by tests or the `pi-news bot dispatch` CLI
command (itself a one-shot, dry-run-only utility, never a running process).

Every dispatch enforces the TELEGRAM_ALLOWED_CHAT_IDS allowlist before any
command handler runs or any private data is returned -- an unapproved chat
gets no response at all (silent ignore, matching design doc section 4.4),
and its command text is never even parsed into an action.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING

from news_intel.delivery import commands
from news_intel.settings import AppConfig

if TYPE_CHECKING:
    from telegram import Update


def is_chat_allowed(chat_id: int, config: AppConfig) -> bool:
    return chat_id in config.secrets.allowed_chat_id_list


def parse_command(text: str) -> tuple[str, list[str]] | None:
    if not text or not text.startswith("/"):
        return None
    parts = text[1:].split()
    if not parts:
        return None
    return parts[0].lower(), parts[1:]


_TOPIC_COMMANDS = {"tech", "market", "china", "us", "world"}


def _dispatch_command(
    conn: sqlite3.Connection, config: AppConfig, command: str, args: list[str], *, now: datetime | None
) -> str:
    if command == "now":
        return commands.cmd_now(conn, config, now=now)
    if command in _TOPIC_COMMANDS:
        return commands.cmd_topic(conn, config, command, now=now)
    if command == "why":
        if not args:
            return "Usage: /why E-..."
        return commands.cmd_why(conn, args[0])
    if command == "sources":
        if not args:
            return "Usage: /sources E-..."
        return commands.cmd_sources(conn, args[0])
    if command == "mute":
        return commands.cmd_mute(conn, args[0] if args else "", now=now)
    if command == "status":
        return commands.cmd_status(conn, config)
    return (
        f"Unknown command /{command}. Try: /now, /tech, /market, /china, /us, /world, "
        "/why, /sources, /mute, /status."
    )


def handle_update(
    conn: sqlite3.Connection, config: AppConfig, update: Update, *, now: datetime | None = None
) -> str | None:
    """Pure dispatch: returns the response text, or None if the chat isn't
    allowlisted or the message isn't a recognized command. Never calls the
    Telegram network API -- there is no running bot process behind this."""
    message = update.effective_message
    if message is None or message.chat is None:
        return None

    chat_id = message.chat.id
    if not is_chat_allowed(chat_id, config):
        return None  # silently ignore, per design doc section 4.4 -- no parsing, no data returned

    parsed = parse_command(message.text or "")
    if parsed is None:
        return None
    command, args = parsed
    return _dispatch_command(conn, config, command, args, now=now)
