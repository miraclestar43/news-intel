"""Event/source-integrity checks (2026-08-13 repair): a dashboard, "latest
numbers" landing page, continuously-updated table, topic page, calendar, or
feed index is not itself a news event. It may be used to discover or
verify a specific release, but selection requires a discrete, source-
supported change (a stated CPI/employment number, a Fed decision, a
company's reported results, an agency action) -- not a lumped list of many
different indicators with no single one distinguished as what happened.

2026-08-12 diagnosis: BLS's "latest numbers" source (bls_latest) always
produces one item titled "Major Economic Indicators Latest Numbers" whose
excerpt is a flat list of ~9 unrelated indicators (CPI, unemployment,
payrolls, ECI, productivity, import/export prices...) refreshed on each
poll -- not a single discrete release. It was selected as a TOP story, and
then falsely "corroborated" by an unrelated Asian-markets commentary
article that merely happened to mention one of the same generic indicator
names ("CPI").
"""

from __future__ import annotations

import re

from news_intel.pipeline.materiality import HIGH_IMPACT_TERMS, dollar_magnitude_bonus

_DASHBOARD_TITLE_PATTERNS = (
    "latest numbers",
    "at a glance",
    "key indicators",
    "economic indicators",
    "dashboard",
    "index page",
    "overview page",
    "summary page",
)

# A discrete release states ONE fact ("CPI rose 0.1% in July"); a dashboard
# lists MANY side by side ("CPI: +0.1% ... Unemployment: 4.1% ... Payroll:
# -23,000 ..."). Matches "Label: value" pairs like the ones bls_latest's
# excerpt actually contains.
_LABELED_METRIC_RE = re.compile(r"[A-Z][A-Za-z /\-()]{2,40}:\s*[+\-]?\$?\d")
_MIN_DISTINCT_METRICS_FOR_DASHBOARD = 3


def is_dashboard_like(title: str, excerpt: str | None) -> bool:
    """True if this looks like an aggregation/landing page rather than a
    discrete, single news event -- title matches a known dashboard-style
    phrase, OR the excerpt lists several distinct labeled metrics side by
    side with no one of them distinguished as the actual story."""
    lowered_title = (title or "").lower()
    if any(pattern in lowered_title for pattern in _DASHBOARD_TITLE_PATTERNS):
        return True
    metric_labels = _LABELED_METRIC_RE.findall(excerpt or "")
    return len(metric_labels) >= _MIN_DISTINCT_METRICS_FOR_DASHBOARD


# 2026-08-13 materiality gate: recognition/award/ranking/listicle language
# ("tops [a] list", "named to", "ranked No. 1", employee-approval-style
# "best CEO" pieces) is corporate self-promotion, not news, unless it comes
# with an independently material consequence. Reproduction: "NVIDIA CEO
# Tops Glassdoor's 2026 List of Best CEOs" (99% employee approval) scored
# competitively under the repaired materiality model purely on category
# base + a percent-magnitude hit from "99%" -- an approval rating, not a
# business result -- and, by outranking genuinely material candidates
# (Cerebras' earnings beat, Decart's acquisition talks) for the AI-category
# cap's two slots, crowded them out of the digest entirely.
_RECOGNITION_TERMS = frozenset(
    {
        "tops the list", "tops glassdoor", "best ceo", "best ceos", "list of the best",
        "named to the", "ranked no. 1", "ranked #1", "power list", "30 under 30",
        "best places to work", "most admired", "hall of fame", "best companies to work",
        "wins award", "receives award", "honored as", "recognized as",
    }
)


def is_unqualified_recognition_story(title: str, excerpt: str | None) -> bool:
    """True if this is recognition/award/ranking language with no
    independently material consequence (leadership change, compensation/
    governance action, measurable business result, market reaction, or
    regulatory consequence) attached. Only the dollar-magnitude and
    decision/consequence-keyword signals count as "material" here --
    deliberately NOT the percent-magnitude signal, since an approval-rating
    or satisfaction-score percentage (exactly what a "best CEO" piece is
    built from) would otherwise self-certify as material."""
    text = f"{title or ''} {excerpt or ''}"
    lowered = text.lower()
    if not any(term in lowered for term in _RECOGNITION_TERMS):
        return False
    has_dollar_magnitude = dollar_magnitude_bonus(text) > 0
    has_high_impact_term = any(term in lowered for term in HIGH_IMPACT_TERMS)
    return not (has_dollar_magnitude or has_high_impact_term)
