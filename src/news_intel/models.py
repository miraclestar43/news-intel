"""Domain models shared across the pipeline, LLM boundary, and delivery layer."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, Field


class ConfirmationState(StrEnum):
    UNVERIFIED = "unverified"
    REPORTED = "reported"
    CONFIRMED = "confirmed"
    OFFICIAL = "official"
    DISPUTED = "disputed"


class Severity(StrEnum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"


class RawItem(BaseModel):
    id: int | None = None
    source_id: str
    external_id: str | None = None
    canonical_url: str
    url_hash: str
    title: str
    normalized_title: str
    summary_excerpt: str | None = None
    language: str = "en"
    published_at: datetime | None = None
    fetched_at: datetime
    content_hash: str
    origin_family: str
    metadata: dict = Field(default_factory=dict)


class ScoreComponents(BaseModel):
    significance: float = 0
    user_relevance: float = 0
    market_impact: float = 0
    confirmation_quality: float = 0
    corroboration: float = 0
    novelty: float = 0
    popularity: float = 0
    freshness: float = 0
    noise_penalty: float = 0
    repetition_penalty: float = 0

    def base_score(self, weights: dict[str, float]) -> float:
        return (
            weights["significance"] * self.significance
            + weights["user_relevance"] * self.user_relevance
            + weights["market_impact"] * self.market_impact
            + weights["confirmation_quality"] * self.confirmation_quality
            + weights["independent_corroboration"] * self.corroboration
            + weights["novelty"] * self.novelty
            + weights["popularity"] * self.popularity
            + weights["freshness"] * self.freshness
        )

    def final_score(self, weights: dict[str, float]) -> float:
        base = self.base_score(weights)
        return max(0.0, min(100.0, base - self.noise_penalty - self.repetition_penalty))


class Event(BaseModel):
    id: str
    primary_title: str
    category: str | None = None
    status: str = "developing"
    confirmation_state: ConfirmationState = ConfirmationState.UNVERIFIED
    first_seen_at: datetime
    last_updated_at: datetime
    published_anchor_at: datetime | None = None
    scores: ScoreComponents = Field(default_factory=ScoreComponents)
    final_score: float = 0
    severity: Severity = Severity.S0
    material_facts: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    llm_enrichment: dict | None = None
    source_raw_item_ids: list[int] = Field(default_factory=list)


# --- LLM provider boundary -------------------------------------------------


class EventInput(BaseModel):
    """What we send to the LLM for enrichment: an event id plus its bounded,
    delimited source excerpts. Never includes internal scores or confirmation
    state — the model only sees facts, not our judgments about them."""

    event_id: str
    candidate_title: str
    sources: list[dict] = Field(description="[{id, publisher, excerpt, published_at}]")


class EventEnrichment(BaseModel):
    event_id: str
    category: str
    subcategories: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    english_headline: str
    what_happened: str
    why_it_matters: str
    chinese_headline: str
    chinese_takeaway: str
    significance_proposal: float = Field(ge=0, le=100)
    market_impact_proposal: float = Field(ge=0, le=100)
    material_facts: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    supporting_source_ids: list[str] = Field(default_factory=list)


class DailyDigestInput(BaseModel):
    date_label: str
    top3_candidates: list[dict]
    quick_brief_candidates: list[dict]
    calendar_candidates: list[dict]


class WeeklyDigestInput(BaseModel):
    week_label: str
    events: list[dict]
    prior_week_summary: dict
    calendar_candidates: list[dict]


class DigestOutput(BaseModel):
    rendered_text: str
    model_used: str
    fallback_used: bool = False
    structured: dict = Field(default_factory=dict)


class ExplanationOutput(BaseModel):
    explanation: str
    chinese_summary: str
    sources_used: list[str] = Field(default_factory=list)


class LLMProvider(Protocol):
    def enrich_events(self, events: list[EventInput]) -> list[EventEnrichment]: ...

    def compose_daily(self, inputs: DailyDigestInput) -> DigestOutput: ...

    def compose_weekly(self, inputs: WeeklyDigestInput) -> DigestOutput: ...

    def explain_event(self, event: EventInput) -> ExplanationOutput: ...
