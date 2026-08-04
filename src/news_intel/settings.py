"""Configuration loading and validation.

Secrets come from an environment file outside the repository
(default: ~/.config/pi-news-intel/pi-news-intel.env, mode 0600).
Non-secret policy/sources/topics come from YAML in config/.
All config is validated at startup via pydantic; invalid config
raises a clear diagnostic instead of running with silent defaults.
"""

from __future__ import annotations

import os
import stat
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent.parent
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_ENV_FILE = Path.home() / ".config" / "pi-news-intel" / "pi-news-intel.env"


class Secrets(BaseSettings):
    """Values that must never be logged, committed, or echoed."""

    model_config = SettingsConfigDict(
        env_file=str(DEFAULT_ENV_FILE) if DEFAULT_ENV_FILE.exists() else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: str | None = Field(default=None, alias="TELEGRAM_ALLOWED_CHAT_IDS")
    llm_base_url: str | None = Field(default=None, alias="LLM_BASE_URL")
    llm_api_key: str | None = Field(default=None, alias="LLM_API_KEY")
    llm_model: str | None = Field(default=None, alias="LLM_MODEL")
    sec_user_agent: str = Field(
        default="PiNewsIntel contact@example.com", alias="SEC_USER_AGENT"
    )
    guardian_api_key: str | None = Field(default=None, alias="GUARDIAN_API_KEY")

    @property
    def allowed_chat_id_list(self) -> list[int]:
        if not self.telegram_allowed_chat_ids:
            return []
        return [int(x.strip()) for x in self.telegram_allowed_chat_ids.split(",") if x.strip()]

    def __repr__(self) -> str:  # never allow accidental secret leakage via repr/log
        return "Secrets(<redacted>)"

    __str__ = __repr__


def env_file_permission_warning() -> str | None:
    """Return a diagnostic string if the secrets env file has unsafe permissions."""
    if not DEFAULT_ENV_FILE.exists():
        return None
    mode = stat.S_IMODE(os.stat(DEFAULT_ENV_FILE).st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return (
            f"{DEFAULT_ENV_FILE} has permissions {oct(mode)}; expected 0600. "
            "Run: chmod 600 " + str(DEFAULT_ENV_FILE)
        )
    return None


class QuietHours(BaseModel):
    start: str
    end: str


class AlertsPolicy(BaseModel):
    s2_daily_budget: int = 3
    minimum_spacing_minutes: int = 30
    same_event_cooldown_hours: int = 6
    burst_merge_minutes: int = 10
    candidate_score: float = 85
    candidate_significance: float = 80
    candidate_market_impact: float = 65
    material_update_novelty: float = 0.35


class DigestsPolicy(BaseModel):
    daily_min_events: int = 4
    daily_max_events: int = 8
    daily_target_english_words: tuple[int, int] = (350, 450)
    daily_target_chars: tuple[int, int] = (2800, 3800)
    weekly_target_english_words: tuple[int, int] = (650, 800)
    weekly_target_chinese_chars: tuple[int, int] = (150, 250)
    telegram_hard_limit_chars: int = 4096
    max_events_per_origin_family: int = 2
    weekly_max_selected_items: int = 12


class ScoringWeights(BaseModel):
    significance: float
    user_relevance: float
    market_impact: float
    confirmation_quality: float
    independent_corroboration: float
    novelty: float
    popularity: float
    freshness: float


class ScoringThresholds(BaseModel):
    alert_candidate: float = 85
    daily_top3: float = 72
    daily_quick_brief: float = 58
    weekly_store: float = 45


class ScoringPolicy(BaseModel):
    weights: ScoringWeights
    thresholds: ScoringThresholds


class DedupePolicy(BaseModel):
    title_similarity_threshold: float = 88
    trigram_jaccard_threshold: float = 0.72
    entity_overlap_threshold: float = 0.50
    candidate_window_hours: int = 48


class StoragePolicy(BaseModel):
    raw_excerpt_max_chars: int = 4000
    raw_excerpt_retention_days: int = 14
    raw_metadata_retention_days: int = 30
    event_retention_days: int = 180
    log_retention_days: int = 14
    log_max_total_mb: int = 50
    backup_retention_days: int = 7


class FetchPolicy(BaseModel):
    default_timeout_seconds: int = 12
    max_retries: int = 2
    max_redirects: int = 3
    max_response_bytes: int = 5_000_000


class TimeSemanticsPolicy(BaseModel):
    """Gate A remediation Phase 2: config-driven bootstrap/event-time policy
    (no source-specific Python). An item with no source-published time, or
    one implausibly far in the future (clock skew / malformed feed data),
    is never given a fabricated "now" anchor -- it is marked archive-only
    and excluded from digest/trend eligibility until a later genuine update
    gives it a valid time."""

    future_skew_tolerance_minutes: int = 60
    archive_only_when_undated: bool = True


class PolicyConfig(BaseModel):
    timezone: str = "America/Los_Angeles"
    collection_interval_minutes: int = 5
    daily_schedule: str = "Mon..Sat 22:00"
    weekly_schedule: str = "Sun 22:00"
    quiet_hours: QuietHours
    alerts: AlertsPolicy
    digests: DigestsPolicy
    scoring: ScoringPolicy
    relevance_multipliers: dict[str, float]
    dedupe: DedupePolicy
    storage: StoragePolicy
    fetch: FetchPolicy
    time_semantics: TimeSemanticsPolicy = Field(default_factory=TimeSemanticsPolicy)

    @field_validator("timezone")
    @classmethod
    def _validate_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"invalid IANA timezone {v!r}: {exc}") from exc
        return v


class SourceEntry(BaseModel):
    id: str
    name: str
    adapter: str
    tier: int
    enabled: bool = True
    url: str = ""
    poll_interval_seconds: int = 300
    category_hints: list[str] = Field(default_factory=list)
    selectors: dict[str, str] | None = None
    requires_identifying_user_agent: bool = False
    note: str | None = None


class SourcesConfig(BaseModel):
    sources: list[SourceEntry]

    @field_validator("sources")
    @classmethod
    def _unique_ids(cls, v: list[SourceEntry]) -> list[SourceEntry]:
        ids = [s.id for s in v]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise ValueError(f"duplicate source ids: {dupes}")
        return v


class TradePolicyContext(BaseModel):
    """Gate A remediation Phase 3.3: bare "tariff" must not by itself imply
    AI/semiconductor or high relevance -- it needs contextual evidence, and
    administrative/utility tariff filings must be excluded outright."""

    anchor_terms_en: list[str] = Field(default_factory=list)
    anchor_terms_zh: list[str] = Field(default_factory=list)
    context_terms_en: list[str] = Field(default_factory=list)
    ai_semiconductor_context_terms_en: list[str] = Field(default_factory=list)
    negative_context_en: list[str] = Field(default_factory=list)


class LegalNoticeBoilerplatePolicy(BaseModel):
    """Gate A remediation Phase 3.4: templated law-firm shareholder-alert
    boilerplate is noise unless a separate material corporate event
    independently qualifies."""

    phrases: list[str] = Field(default_factory=list)
    material_override_phrases: list[str] = Field(default_factory=list)


class TrendIdentityPolicy(BaseModel):
    """Gate A remediation Phase 4: a multi-event trend requires a specific,
    non-generic shared anchor -- these broad terms can never justify a
    multi-event grouping on their own, no matter how often they co-occur."""

    generic_anchor_terms: list[str] = Field(default_factory=list)
    topic_trigram_floor: float = 0.12


class TopicsConfig(BaseModel):
    groups: dict[str, dict]
    noise_exclusions: list[str] = Field(default_factory=list)
    trade_policy_context: TradePolicyContext = Field(default_factory=TradePolicyContext)
    legal_notice_boilerplate: LegalNoticeBoilerplatePolicy = Field(default_factory=LegalNoticeBoilerplatePolicy)
    trend_identity: TrendIdentityPolicy = Field(default_factory=TrendIdentityPolicy)


class AlertTriggerEntry(BaseModel):
    id: str
    severity_ceiling: str
    market_impact_exempt: bool = False
    is_statement: bool = False
    terms_en: list[str] = Field(default_factory=list)
    terms_zh: list[str] = Field(default_factory=list)

    @field_validator("severity_ceiling")
    @classmethod
    def _valid_ceiling(cls, v: str) -> str:
        if v not in ("S2", "S3"):
            raise ValueError(f"severity_ceiling must be S2 or S3, got {v!r}")
        return v


class AlertTriggersConfig(BaseModel):
    triggers: list[AlertTriggerEntry]


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"missing required config file: {path}")
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class AppConfig(BaseModel):
    policy: PolicyConfig
    sources: SourcesConfig
    topics: TopicsConfig
    alert_triggers: AlertTriggersConfig
    secrets: Secrets
    config_dir: Path
    data_dir: Path

    model_config = {"arbitrary_types_allowed": True}


@lru_cache
def load_config(config_dir: Path | None = None, data_dir: Path | None = None) -> AppConfig:
    config_dir = config_dir or DEFAULT_CONFIG_DIR
    data_dir = data_dir or DEFAULT_DATA_DIR
    data_dir.mkdir(parents=True, exist_ok=True)

    policy = PolicyConfig.model_validate(_load_yaml(config_dir / "policy.yaml"))
    sources = SourcesConfig.model_validate(_load_yaml(config_dir / "sources.yaml"))
    topics = TopicsConfig.model_validate(_load_yaml(config_dir / "topics.yaml"))
    alert_triggers = AlertTriggersConfig.model_validate(_load_yaml(config_dir / "alert_triggers.yaml"))
    secrets = Secrets()

    return AppConfig(
        policy=policy,
        sources=sources,
        topics=topics,
        alert_triggers=alert_triggers,
        secrets=secrets,
        config_dir=config_dir,
        data_dir=data_dir,
    )


def reset_config_cache() -> None:
    load_config.cache_clear()
