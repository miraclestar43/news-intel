"""Publisher-family normalization (2026-08-12 content-quality repair).

2026-08-11 diagnosis: `blogs.nvidia.com` and `developer.nvidia.com` were
treated as two different domains by the per-source diversity cap (exact
hostname match), so both counted toward separate TOP-story slots even
though they are the same publisher. Diversity limits must operate on the
publisher's registrable (eTLD+1) domain, not the full hostname.

No external public-suffix dependency is available in this environment, so
this is a small, explicit multi-part-suffix list covering the domains this
project actually collects from (US .gov agencies are each their own
registrable domain and need no special-casing; a handful of two-label
country-code suffixes -- e.g. .gov.cn, .co.uk -- are not) plus the standard
two-label fallback. It is deliberately conservative: an unrecognized
multi-part suffix just falls back to the generic two-label rule, which is
correct for the overwhelming majority of real-world domains including every
source currently in config/sources.yaml.
"""

from __future__ import annotations

from urllib.parse import urlparse

_MULTI_PART_SUFFIXES = frozenset(
    {
        "gov.cn", "com.cn", "org.cn", "net.cn", "edu.cn",
        "co.uk", "org.uk", "ac.uk", "gov.uk", "net.uk",
        "com.au", "gov.au", "org.au",
        "co.jp", "or.jp", "ne.jp",
        "com.br", "co.kr", "co.in",
    }
)


def registrable_domain(host: str) -> str:
    """The publisher-family key for a hostname: `blogs.nvidia.com` and
    `developer.nvidia.com` both -> `nvidia.com`; `pbc.gov.cn` and
    `www.gov.cn` stay distinct (different organizations under a shared
    country-code government suffix)."""
    host = (host or "").lower().strip().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_PART_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def publisher_family_of_url(url: str | None) -> str:
    if not url:
        return ""
    return registrable_domain(urlparse(url).netloc)
