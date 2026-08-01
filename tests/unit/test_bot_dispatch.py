"""Phase 2 completion item 3: Telegram query commands wired to a dispatcher
that understands real `telegram.Update` payloads, with the
TELEGRAM_ALLOWED_CHAT_IDS allowlist enforced before any command runs or any
data is returned. No token is required -- Update.de_json() is pure parsing,
and no network call is made anywhere in this file. pi-news-bot.service is
never installed, enabled, or started."""

from __future__ import annotations

from datetime import UTC, datetime

from telegram import Update

from news_intel.delivery.bot import handle_update, is_chat_allowed, parse_command
from news_intel.runtime_state import MUTE_UNTIL_KEY
from news_intel.runtime_state import get as runtime_get
from tests.acceptance.test_phase2_alert_scenarios import _seed_event

ALLOWED_CHAT_ID = 12345
OTHER_CHAT_ID = 99999


def _update(chat_id: int, text: str) -> Update:
    payload = {
        "update_id": 1,
        "message": {
            "message_id": 1,
            "date": 0,
            "chat": {"id": chat_id, "type": "private"},
            "text": text,
        },
    }
    return Update.de_json(payload, bot=None)


def _allow(config, chat_id: int) -> None:
    config.secrets.telegram_allowed_chat_ids = str(chat_id)


def test_parse_command_variants():
    assert parse_command("/now") == ("now", [])
    assert parse_command("/why E-20260801-0001") == ("why", ["E-20260801-0001"])
    assert parse_command("not a command") is None
    assert parse_command("") is None


def test_is_chat_allowed(config):
    _allow(config, ALLOWED_CHAT_ID)
    assert is_chat_allowed(ALLOWED_CHAT_ID, config) is True
    assert is_chat_allowed(OTHER_CHAT_ID, config) is False


def test_allowed_chat_routed_correctly_for_now(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BOT-1", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_bot_1", 0, "bis.gov")], now=now,
    )
    conn.execute("UPDATE events SET final_score=90 WHERE id='E-BOT-1'")

    update = _update(ALLOWED_CHAT_ID, "/now")
    response = handle_update(conn, config, update, now=now)

    assert response is not None
    assert "E-BOT-1" in response


def test_allowed_chat_routed_for_all_required_commands(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BOT-2", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_bot_2", 0, "bis.gov")], now=now,
    )
    conn.execute("UPDATE events SET final_score=90, category='AI & Semiconductors' WHERE id='E-BOT-2'")

    for text in ("/now", "/tech", "/market", "/china", "/us", "/world", "/why E-BOT-2", "/sources E-BOT-2", "/mute 2h"):
        response = handle_update(conn, config, _update(ALLOWED_CHAT_ID, text), now=now)
        assert response is not None, f"expected a response for {text!r}"
        assert "Unknown command" not in response


def test_unapproved_chat_cannot_query(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)
    _seed_event(
        conn, event_id="E-BOT-3", title="BIS export control rule on chips",
        excerpt="BIS announced an export control rule on chips.",
        sources=[("bis_bot_3", 0, "bis.gov")], now=now,
    )
    conn.execute("UPDATE events SET final_score=90 WHERE id='E-BOT-3'")

    response = handle_update(conn, config, _update(OTHER_CHAT_ID, "/now"), now=now)
    assert response is None


def test_unapproved_chat_cannot_mutate_state(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    now = datetime(2026, 7, 31, 15, 0, tzinfo=UTC)

    response = handle_update(conn, config, _update(OTHER_CHAT_ID, "/mute 2h"), now=now)

    assert response is None
    assert runtime_get(conn, MUTE_UNTIL_KEY) is None  # the mute was never actually applied


def test_unrecognized_command_from_allowed_chat_gets_help_text(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    response = handle_update(conn, config, _update(ALLOWED_CHAT_ID, "/bogus"))
    assert response is not None
    assert "Unknown command" in response


def test_non_command_message_gets_no_response(config, conn):
    _allow(config, ALLOWED_CHAT_ID)
    response = handle_update(conn, config, _update(ALLOWED_CHAT_ID, "just chatting, not a command"))
    assert response is None
