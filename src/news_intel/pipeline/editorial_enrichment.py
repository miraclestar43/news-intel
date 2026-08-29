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

from pydantic import ValidationError

from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.fallback import _complete_sentence_summary
from news_intel.models import EventEnrichment, EventInput
from news_intel.pipeline.editorial_evidence import build_event_input
from news_intel.pipeline.editorial_validation import validate_enrichment
from news_intel.pipeline.summary_quality import best_available_summary
from news_intel.pipeline.zh_render_readiness import what_happened_zh

_STORY_TIMEOUT_SECONDS = 12.0
# 2026-08-24: hermes_cli pays a fixed subprocess/interpreter-startup cost
# (observed ~10s round-trip for a TRIVIAL prompt against the real CLI) that
# the HTTP path doesn't -- the same 12s budget that's generous for a chat-
# completions POST would starve almost every hermes_cli call before the
# model even finishes. A live 4-digest replay against real production
# evidence packets at a 30s bound still timed out on 7 of 8 real stories
# (successful calls observed at 23-30s even before adding retry time) --
# raised to 60s based on that measurement. Still a strict, bounded
# ceiling, just calibrated to this provider's real observed latency
# rather than an untested guess.
_HERMES_CLI_STORY_TIMEOUT_SECONDS = 60.0

# 2026-08-29 production incident: a per-CALL timeout alone does not bound
# the JOB. enrich_story() makes up to 2 provider attempts, and
# HermesCliProvider._complete() internally makes up to 2 subprocess calls
# (one JSON-repair retry), so a single story's worst case is 2 x 2 x 60s =
# 240s, and a 3-story TOP section is 720s -- against a systemd
# TimeoutStartSec of 280s on both daily units. A run that hit the worst
# case would be SIGTERMed mid-flight, producing no digest, no delivery and
# a truncated run record. This is a whole-run deadline: once it passes,
# every remaining story goes straight to the source-grounded floor without
# calling the provider at all, so the editorial layer can never push the
# job past its service budget. Sized to leave headroom for selection,
# rendering, Telegram, and process teardown inside 280s.
EDITORIAL_RUN_BUDGET_SECONDS = 150.0


def build_bounded_provider(config):
    """Constructs a per-story provider with a STRICTER timeout than the
    whole-digest compose_daily/compose_weekly path uses (30s default) --
    a single-event call is small enough that 12s is generous, and a
    stuck/slow model must not stall the whole digest job. Reads only
    pi-news-intel's OWN config.secrets (the exact same surface
    llm.provider.get_provider() dispatches on -- LLM_PROVIDER=hermes_cli
    plus HERMES_BIN/HERMES_TIMEOUT_SECONDS, or LLM_BASE_URL/LLM_API_KEY/
    LLM_MODEL for the HTTP path) -- never Hermes's own configuration or
    credential files. Returns None (never raises) when unconfigured,
    matching NullProvider's trigger condition, so enrich_story() falls
    straight to the source-grounded floor."""
    secrets = config.secrets
    if secrets.llm_provider == "hermes_cli":
        if not secrets.hermes_bin:
            return None
        from news_intel.llm.hermes_cli import HermesCliProvider

        story_timeout = min(
            secrets.hermes_timeout_seconds or _HERMES_CLI_STORY_TIMEOUT_SECONDS, _HERMES_CLI_STORY_TIMEOUT_SECONDS
        )
        return HermesCliProvider(
            bin_path=secrets.hermes_bin, timeout=story_timeout, model_label=secrets.hermes_model_label
        )
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
    # 2026-08-24: a Chinese "what happened" sentence, distinct from
    # chinese_takeaway (why it matters) -- the zh_cn renderer reads THIS
    # field, never the English what_happened/why_it_matters, so a Chinese
    # digest can never end up displaying English prose.
    chinese_what_happened: str | None
    chinese_takeaway: str | None
    category: str | None
    source: str  # "llm_validated" | "deterministic_fallback"
    model_used: str | None = None
    validated: bool = False
    fallback_reason: str | None = None
    latency_ms: float | None = None
    retry_count: int = 0


def _source_grounded_fallback(primary: dict, secondary: dict | None, reason: str) -> EditorialResult:
    """The floor Stage B/Stage C requires: a concise, complete, source-
    grounded WHAT HAPPENED sentence (in both languages, since either
    renderer may need it), and why_it_matters/chinese_takeaway left as
    None so the renderer OMITS the line entirely -- never a generic
    placeholder or category-only template."""
    what_happened = best_available_summary(primary, secondary)
    if what_happened is None:
        excerpt = primary.get("excerpt") or (secondary or {}).get("excerpt")
        what_happened = _complete_sentence_summary(excerpt)
    chinese_what_happened = what_happened_zh(primary, secondary)
    return EditorialResult(
        what_happened=what_happened, why_it_matters=None, chinese_what_happened=chinese_what_happened,
        chinese_takeaway=None, category=None, source="deterministic_fallback", fallback_reason=reason,
    )


def enrich_story(
    primary: dict, secondary: dict | None, category: str, entities: list[str] | None,
    *, edition_id: str = "en_global", update_note: str | None = None, provider=None,
    deadline: float | None = None,
) -> EditorialResult:
    """`provider` is injected (never constructed here from config) so
    callers control exactly what -- if anything -- this can reach; tests
    always pass a fake. A None/unconfigured provider is the same as any
    other failure mode: falls straight to the source-grounded floor.

    `deadline` is a time.monotonic() instant after which this story is NOT
    sent to the provider at all (see EDITORIAL_RUN_BUDGET_SECONDS) -- the
    whole-run bound that keeps a slow/timing-out provider from pushing the
    job past its systemd TimeoutStartSec."""
    if provider is None:
        return _source_grounded_fallback(primary, secondary, "no provider configured")
    if deadline is not None and time.monotonic() >= deadline:
        return _source_grounded_fallback(primary, secondary, "editorial run budget exhausted")

    evidence = build_event_input(
        primary, secondary, category, entities, edition_id=edition_id, update_note=update_note
    )

    start = time.monotonic()
    last_reasons: list[str] = []
    retry_count = 0
    for _attempt in range(2):  # one original attempt + one validation-error-guided retry
        # A validation-guided retry costs another full provider timeout;
        # skip it (keeping the already-collected rejection reasons) once
        # the whole-run budget is gone, rather than overrunning the job.
        if _attempt > 0 and deadline is not None and time.monotonic() >= deadline:
            break
        try:
            enrichment = _call_provider_bounded(provider, evidence, last_reasons)
        except LLMUnavailableError as exc:
            latency_ms = (time.monotonic() - start) * 1000
            result = _source_grounded_fallback(primary, secondary, f"provider unavailable: {exc}")
            result.latency_ms = latency_ms
            result.retry_count = retry_count
            return result

        validation = validate_enrichment(enrichment, evidence)
        if validation.ok:
            latency_ms = (time.monotonic() - start) * 1000
            return EditorialResult(
                what_happened=enrichment.what_happened,
                why_it_matters=enrichment.why_it_matters,
                chinese_what_happened=enrichment.chinese_what_happened,
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
        primary, secondary, f"validation failed after retry: {'; '.join(last_reasons)}"
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
    # 2026-08-25 repair: real live-model replay crash -- a real Hermes/MiMo
    # response was syntactically valid JSON (so the provider's own internal
    # invalid-JSON retry never triggered) but missing several required
    # EventEnrichment fields. EventEnrichment.model_validate() inside
    # enrich_events() raised pydantic.ValidationError, which propagated
    # UNCAUGHT past this function -- aborting the entire daily job instead
    # of falling back for just this one story, exactly the failure mode
    # the retry/fallback contract exists to prevent. Any malformed-but-
    # JSON-parseable response (a missing field, or a non-dict top level
    # that breaks data["event_id"] = ... inside a provider) must degrade
    # the same way an invalid-JSON or unreachable-provider response does.
    try:
        results = provider.enrich_events([evidence])
    except (ValidationError, KeyError, TypeError) as exc:
        raise LLMUnavailableError(f"provider returned a malformed enrichment: {exc}") from exc
    if not results:
        raise LLMUnavailableError("provider returned no enrichment")
    return results[0]
