"""Runtime context passed to adapters: a SourceEntry plus resolved policy
defaults, so adapters never read global config directly (keeps them testable
in isolation with fixtures)."""

from __future__ import annotations

from dataclasses import dataclass, field

from news_intel.settings import AppConfig, SourceEntry


@dataclass
class SourceContext:
    id: str
    name: str
    url: str
    adapter: str
    tier: int
    poll_interval_seconds: int
    fetch_timeout: float
    excerpt_max_chars: int
    default_language: str
    user_agent: str
    selectors: dict[str, str] | None = None
    category_hints: list[str] = field(default_factory=list)
    note: str | None = None


def build_context(entry: SourceEntry, config: AppConfig, default_language: str = "en") -> SourceContext:
    user_agent = "PiNewsIntel/0.1 (+personal use)"
    if entry.requires_identifying_user_agent:
        user_agent = config.secrets.sec_user_agent
    return SourceContext(
        id=entry.id,
        name=entry.name,
        url=entry.url,
        adapter=entry.adapter,
        tier=entry.tier,
        poll_interval_seconds=entry.poll_interval_seconds,
        fetch_timeout=config.policy.fetch.default_timeout_seconds,
        excerpt_max_chars=config.policy.storage.raw_excerpt_max_chars,
        default_language=default_language,
        user_agent=user_agent,
        selectors=entry.selectors,
        category_hints=entry.category_hints,
        note=entry.note,
    )
