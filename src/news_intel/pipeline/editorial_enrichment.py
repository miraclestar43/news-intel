"""Orchestrates the optional per-story LLM editorial layer (2026-08-24):
build the bounded evidence packet, call the provider once, validate,
retry at most once with the validation errors fed back, and fall back to a
strictly source-grounded deterministic floor (what_happened only -- never
a generic why_it_matters) if both attempts fail or the provider is
unavailable/times out.

Safety properties (all structural, not configuration-dependent):
  - the provider is given NO tools, NO browsing, and no shell/file
    instructions -- see llm/openai_compatible.py's system prompt and the
    fact that EventInput carries only text/numbers, never executable
    content or credentials;
  - article/source text is delimited as untrusted <source> data (prompts.
    render_sources_block), never concatenated as instructions;
  - the provider object here is never given a chat_id, TelegramSender, or
    any other delivery capability -- it cannot itself contact Telegram
    under any circumstance, independent of jobs/delivery_authorization.py;
  - telemetry records only the model NAME (a config value pi-news-intel
    itself set, not a secret) plus validation/fallback/latency -- never
    the request/response body, api key, or base_url.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.fallback import _complete_sentence_summary
from news_intel.models import EventEnrichment, EventInput
from news_intel.pipeline.editorial_evidence import build_event_input
from news_intel.pipeline.editorial_validation import validate_enrichment
from news_intel.pipeline.summary_quality import best_available_summary
from news_intel.pipeline.zh_render_readiness import what_happened_zh

_STORY_TIMEOUT_SECONDS = 12.0


def build_bounded_provider(config):
    """Constructs a per-story provider with a STRICTER timeout than the
    whole-digest compose_daily/compose_weekly path uses (30s default) --
    a single-event call is small enough that 12s is generous, and a
    stuck/slow model must not stall the whole digest job. Reads only
    pi-news-intel's OWN config.secrets (LLM_BASE_URL/LLM_API_KEY/LLM_MODEL,
    the exact same surface get_provider() already uses) -- never Hermes's
    own configuration or credential files. Returns None (never raises)
    when unconfigured, matching NullProvider's trigger condition, so
    enrich_story() falls straight to the source-grounded floor."""
    secrets = config.secrets
    if not (secrets.llm_base_url and secrets.llm_api_key and secrets.llm_model):
        return None
    from news_intel.llm.openai_compatible import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        base_url=secrets.llm_base_url, api_key=secrets.llm_api_key, model=secrets.llm_model,
        timeout=_STORY_TIMEOUT_SECONDS,
    )


@dataclass
class EditorialResult:
    """What callers (jobs/daily.py, the renderers) actually consume.
    `source` distinguishes the two and only two provenances a reader-facing
    line can have -- never a third, ambiguous state."""

    what_happened: str | None
    why_it_matters: str | None
    chinese_takeaway: str | None
    category: str | None
    source: str  # "llm_validated" | "deterministic_fallback"
    model_used: str | None = None
    validated: bool = False
    fallback_reason: str | None = None
    latency_ms: float | None = None
    retry_count: int = 0


def _source_grounded_fallback(primary: dict, secondary: dict | None, edition_id: str, reason: str) -> EditorialResult:
    """The floor Stage B/Stage C requires: a concise, complete, source-
    grounded WHAT HAPPENED sentence, and why_it_matters/chinese_takeaway
    left as None so the renderer OMITS the line entirely -- never a
    generic placeholder or category-only template."""
    if edition_id == "zh_cn":
        what_happened = what_happened_zh(primary, secondary)
    else:
        what_happened = best_available_summary(primary, secondary)
        if what_happened is None:
            excerpt = primary.get("excerpt") or (secondary or {}).get("excerpt")
            what_happened = _complete_sentence_summary(excerpt)
    return EditorialResult(
        what_happened=what_happened, why_it_matters=None, chinese_takeaway=None, category=None,
        source="deterministic_fallback", fallback_reason=reason,
    )


def enrich_story(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None,
    *, edition_id: str = "en_global", update_note: str | None = None, provider=None,
) -> EditorialResult:
    """`provider` is injected (never constructed here from config) so
    callers control exactly what -- if anything -- this can reach; tests
    always pass a fake. A None/unconfigured provider is the same as any
    other failure mode: falls straight to the source-grounded floor."""
    if provider is None:
        return _source_grounded_fallback(primary, secondary, edition_id, "no provider configured")

    evidence = build_event_input(
        primary, secondary, category, entities, edition_id=edition_id, update_note=update_note
    )

    start = time.monotonic()
    last_reasons: list[str] = []
    retry_count = 0
    for _attempt in range(2):  # one original attempt + one validation-error-guided retry
        try:
            enrichment = _call_provider_bounded(provider, evidence, last_reasons)
        except LLMUnavailableError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            result = _source_grounded_fallback(primary, secondary, edition_id, f"provider unavailable: {exc}")
            result.latency_ms = latency_ms
            result.retry_count = retry_count
            return result

        validation = validate_enrichment(enrichment, evidence)
        if validation.ok:
            latency_ms = (time.monotonic() - start) * 1000
            return EditorialResult(
                what_happened=enrichment.what_happened,
                why_it_matters=enrichment.why_it_matters,
                chinese_takeaway=enrichment.chinese_takeaway,
                category=enrichment.category,
                source="llm_validated",
                model_used=getattr(provider, "model", None),
                validated=True,
                latency_ms=latency_ms,
                retry_count=retry_count,
            )
        last_reasons = validation.reasons
        retry_count += 1

    latency_ms = (time.monotonic() - start) * 1000
    result = _source_grounded_fallback(
        primary, secondary, edition_id, f"validation failed after retry: {'; '.join(last_reasons)}"
    )
    result.model_used = getattr(provider, "model", None)
    result.latency_ms = latency_ms
    result.retry_count = retry_count
    return result


def _call_provider_bounded(provider, evidence: EventInput, prior_errors: list[str]) -> EventEnrichment:
    """A single enrich_events() call for exactly one event -- bounded input
    (one event, its own sources only), a strict timeout enforced by the
    provider's own httpx.Client(timeout=...) (see OpenAICompatibleProvider),
    and, on retry, the prior validation errors appended so the model can
    self-correct within the SAME contract rather than being asked to
    guess differently."""
    if prior_errors:
        # A validation-guided retry: mutate a copy of the sources list to
        # append a final, clearly-delimited correction instruction as an
        # extra source-less directive -- never edits article text itself.
        evidence = evidence.model_copy(
            update={
                "candidate_title": evidence.candidate_title
                + " [PRIOR ATTEMPT REJECTED: " + "; ".join(prior_errors)[:400] + "]"
            }
        )
    results = provider.enrich_events([evidence])
    if not results:
        raise LLMUnavailableError("provider returned no enrichment")
    return results[0]
