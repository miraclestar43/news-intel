from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from news_intel import db  # noqa: E402
from news_intel.adapters.context import SourceContext  # noqa: E402
from news_intel.settings import load_config, reset_config_cache  # noqa: E402


@pytest.fixture
def config(tmp_path, monkeypatch):
    for var in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_CHAT_IDS",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "LLM_PROVIDER",
        "HERMES_BIN",
        "HERMES_TIMEOUT_SECONDS",
        "HERMES_MODEL_LABEL",
        "GUARDIAN_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    reset_config_cache()
    cfg = load_config(config_dir=PROJECT_ROOT / "config", data_dir=tmp_path / "data")
    yield cfg
    reset_config_cache()


@pytest.fixture
def conn(config):
    connection = db.connect(config.data_dir / "news-intel.db")
    db.run_migrations(connection)
    yield connection
    connection.close()


def make_source_context(
    *,
    id: str = "test_source",  # noqa: A002
    name: str = "Test Source",
    url: str = "https://example.com/feed",
    adapter: str = "rss",
    tier: int = 1,
    excerpt_max_chars: int = 4000,
    selectors: dict | None = None,
    note: str | None = None,
) -> SourceContext:
    return SourceContext(
        id=id,
        name=name,
        url=url,
        adapter=adapter,
        tier=tier,
        poll_interval_seconds=300,
        fetch_timeout=12.0,
        excerpt_max_chars=excerpt_max_chars,
        default_language="en",
        user_agent="PiNewsIntel/test",
        selectors=selectors,
        note=note,
    )


def insert_source_row(conn, source_id: str, tier: int, adapter_type: str = "rss", name: str | None = None) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO sources (id, name, adapter_type, tier, enabled, url, "
        "category_hints_json, poll_interval_seconds, health_state, config_hash) "
        "VALUES (?, ?, ?, ?, 1, 'https://example.com', '[]', 300, 'healthy', 'test')",
        (source_id, name or source_id, adapter_type, tier),
    )
