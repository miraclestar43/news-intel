"""TOP-story render-readiness gate (2026-08-15 content-quality repair).

2026-08-14 defect: "(No complete source-supported summary available -- see
original link.)" -- an internal diagnostic string, not reader-facing
content -- was rendered directly into a delivered TOP story. Round C
(2026-08-13) had deliberately made summary availability NOT a TOP-
*eligibility* gate, so a genuinely material story wouldn't be demoted
below a less material one just because its excerpt was poorly formatted;
that decision stands (decision-value ordering is computed and stays fixed
before enrichment is attempted -- see pipeline/daily_selection.py). What
was missing is a check *after* ordering, before rendering: if, having tried
every available source, no complete account can be produced at all, that
story must not occupy the slot with a placeholder -- it fails render-
readiness and the next qualified, render-ready story takes the slot
instead.

Each TOP story is expected to carry four things before it reaches the
renderer: what happened (the existing source-supported summary sentence),
why it matters (below), a Chinese takeaway (below), and a truthful
clickable source (event_repo's `display_url` -- see the publisher-family
repair). The latter two are deterministic, template-based syntheses of
already-verified structured facts already computed elsewhere in the
pipeline (category, parsed dollar magnitude) -- never invented detail, and
never blocked on an LLM, since none is configured in this deployment. They
are documented as a floor, not a claim of genuine editorial analysis or
translation: with no LLM and no full-article fetch in this pipeline, a
distinct, source-grounded rationale/translation beyond the source's own
excerpt is not achievable here -- see the round's checkpoint report.
"""

from __future__ import annotations

from news_intel.pipeline.materiality import _parse_dollar_values, format_dollar_value
from news_intel.pipeline.summary_quality import best_available_summary

_CATEGORY_CONTEXT_EN = {
    "AI & Semiconductors": "AI/semiconductor infrastructure and competitiveness",
    "Markets & Economy": "corporate finances or the broader economy",
    "Politics": "domestic policy",
    "US / China / World": "US-China relations or global affairs",
    "Technology": "the technology industry",
}

_CATEGORY_CONTEXT_ZH = {
    "AI & Semiconductors": "AI/半导体产业格局",
    "Markets & Economy": "企业财务或宏观经济",
    "Politics": "国内政策",
    "US / China / World": "中美关系或全球局势",
    "Technology": "科技行业",
}


def _combined_text(primary: dict, secondary: dict | None) -> str:
    parts = [f"{primary.get('title', '')} {primary.get('excerpt', '') or ''}"]
    if secondary:
        parts.append(f"{secondary.get('title', '')} {secondary.get('excerpt', '') or ''}")
    return " ".join(parts)


def is_render_ready(primary: dict, secondary: dict | None) -> bool:
    """True once enrichment (primary source, then linked secondary source
    -- pipeline/summary_quality.best_available_summary, decision-value
    order) has been attempted and produced a complete, source-supported
    "what happened" sentence. False means every available source was tried
    and none yielded one -- the render-readiness gate then fails this
    story rather than rendering a placeholder."""
    return best_available_summary(primary, secondary) is not None


def why_it_matters(primary: dict, secondary: dict | None, category: str) -> str:
    """A deterministic, template-based one-line rationale built only from
    already-verified structured facts (category, largest parsed dollar
    figure in the event's own text) -- never invents detail. This is a
    floor, not genuine editorial analysis: producing a distinct,
    source-grounded "why it matters" clause beyond what the source's own
    (typically single-sentence) excerpt already says would require either
    an LLM or full-article fetching, neither of which exists in this
    deployment -- documented as a known limitation, not overclaimed."""
    context = _CATEGORY_CONTEXT_EN.get(category, "this coverage area")
    values = _parse_dollar_values(_combined_text(primary, secondary))
    if values:
        amount = format_dollar_value(max(values))
        return f"Why it matters: a {amount} development in {context}."
    return f"Why it matters: ranks among today's most material developments in {context}."


def chinese_takeaway(category: str) -> str:
    """A minimal, deterministic Chinese-language takeaway naming only the
    event's own (already-verified) category -- not a translation of the
    story's specific content, which would require an LLM this deployment
    does not have. Documented as a floor, not a claim of full translation."""
    context = _CATEGORY_CONTEXT_ZH.get(category, "本次报道领域")
    return f"看点：{context}的重要动态。"
