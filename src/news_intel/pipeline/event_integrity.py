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
