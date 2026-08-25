"""A small, curated Chinese<->English alias table for entities that
commonly appear in financial/tech news under a different name per
language (2026-08-25). Not a translation engine: a bounded, general-
purpose lookup for well-known organizations, reused by the editorial
evidence packet (offer both forms as citable facts) and the editorial
validator (accept either form as traceable) -- the SAME underlying entity
being named in a different script is not a fabrication, and a real
live-model replay showed a Chinese source excerpt naming banks in Chinese
("高盛、摩根士丹利及摩根大通") get correctly summarized in English
("Goldman Sachs, Morgan Stanley, JPMorgan") and rejected as "not
traceable" purely because the two scripts don't literally substring-match.
"""

from __future__ import annotations

# Keys and values are the canonical display forms; lookups are
# case/script-normalized by the caller.
ENTITY_ALIASES: dict[str, str] = {
    "goldman sachs": "高盛",
    "morgan stanley": "摩根士丹利",
    "jpmorgan": "摩根大通",
    "jp morgan": "摩根大通",
    "ubs": "瑞银",
    "hong kong stock exchange": "港交所",
    "hkex": "港交所",
    "tencent": "腾讯",
    "alibaba": "阿里巴巴",
    "nvidia": "英伟达",
    "netease": "网易",
    "netease youdao": "网易有道",
    "youdao": "有道",
    "taikang life": "泰康人寿",
    "taikang life insurance": "泰康人寿",
    "luxshare": "立讯精密",
    "luxshare precision": "立讯精密",
    "luxshare limited": "立讯精密",
}

# Multi-valued: several English forms commonly map to the SAME Chinese
# name (e.g. "Luxshare"/"Luxshare Precision"/"Luxshare Limited" all mean
# 立讯精密) -- a single-valued reverse dict would only ever recover
# whichever English key happened to be inserted last.
_REVERSE_ALIASES: dict[str, set[str]] = {}
for _en, _zh in ENTITY_ALIASES.items():
    _REVERSE_ALIASES.setdefault(_zh, set()).add(_en)


def alias_variants(name: str) -> set[str]:
    """All known-equivalent lowercased forms of `name`, including itself."""
    lowered = name.strip().lower()
    variants = {lowered}
    if lowered in ENTITY_ALIASES:
        variants.add(ENTITY_ALIASES[lowered])
    # Chinese keys aren't meaningfully .lower()'d, so also check the
    # original (unlowered) string against the reverse table.
    original = name.strip()
    variants |= _REVERSE_ALIASES.get(original, set())
    variants |= _REVERSE_ALIASES.get(lowered, set())
    return variants
