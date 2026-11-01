"""Chinese <-> English entity equivalences for the editorial evidence packet and validator (2026-08-25; 2026-11-01).

Not a translation engine: a bounded, reviewable lookup. Since 2026-11-01 the table lives in config/entity_aliases.yaml
and every entry carries its basis -- a collected source that itself wrote the pair ("马斯克（Elon Musk）", with the
article URL), the organisation's own Chinese-language site, or the 2026-08-25 live-model replay that added the
original entries. The SAME entity named in a different script is not a fabrication (a Chinese source's "高盛" summarised
in English as "Goldman Sachs"); a name with no entry and no occurrence in the sources is still rejected.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml

ALIASES_PATH = Path(__file__).resolve().parents[3] / "config" / "entity_aliases.yaml"


@lru_cache(maxsize=1)
def _tables() -> tuple[dict[str, set[str]], list[dict]]:
    entries = (yaml.safe_load(ALIASES_PATH.read_text(encoding="utf-8")) or {}).get("aliases") or []
    table: dict[str, set[str]] = {}
    for entry in entries:
        if not entry.get("basis") or not entry.get("ref"):
            raise ValueError(f"entity alias without a reviewable basis: {entry}")
        names = {entry["zh"].strip(), *(e.strip().lower() for e in entry["en"])}
        for name in names:
            table.setdefault(name.lower(), set()).update(n.lower() for n in names)
    return table, entries


def alias_entries() -> list[dict]:
    """The reviewable entries, with their basis (for reports)."""
    return list(_tables()[1])


def alias_variants(name: str) -> set[str]:
    """All known-equivalent lowercased forms of `name`, including itself."""
    lowered = name.strip().lower()
    return {lowered} | _tables()[0].get(lowered, set())
