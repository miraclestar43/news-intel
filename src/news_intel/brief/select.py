"""The editor's choice: merge duplicates across editions and languages, choose by importance (2026-11-01).

The deterministic pre-ranking (pool.py) cannot tell that a BBC report and a 中国新闻网 report are the same event, or
weigh a ceasefire against a chip-maker's earnings. One editor call to the same Claude CLI provider does both, from the
candidates' own titles and first lines, and must say why it chose each item. Its answer is checked: only known ids,
no id in two items, at most `max_items`. If the call fails, the deterministic order is used instead (one item per
duplicate title, at most two per bucket) and the snapshot says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from news_intel.brief.pool import Candidate
from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.prompts import _attr, _data, load_template, render

MAX_ITEMS = 5
MAX_RESERVES = 4


@dataclass
class Pick:
    candidates: list[Candidate]  # first = primary (best pre-rank), rest = same-event duplicates
    reason: str
    reserve: bool = False


@dataclass
class Selection:
    method: str  # "editor_model" | "deterministic_fallback"
    picks: list[Pick]
    reserves: list[Pick]
    not_chosen: list[dict] = field(default_factory=list)
    model: str | None = None
    error: str | None = None
    latency_ms: int | None = None


def _line(c: Candidate) -> str:
    srcs = sorted({s.get("publisher") or "?" for s in c.event["sources"]})
    first = re.sub(r"\s+", " ", c.event.get("excerpt") or "")[:140]
    prev = (f"; previously in the brief {c.previously['edition_date']}: {c.previously['headline']}"
            if c.previously else "")
    return (f'[{c.key}] ({_attr(", ".join(srcs[:3]))}; {c.first_published:%m-%d %H:%M} UTC{_attr(prev)})\n'
            f"    {_data(c.title)} | {_data(first)}")


def build_select_prompt(cands: list[Candidate], edition, max_items: int = MAX_ITEMS) -> str:
    return render(
        load_template("select_brief.md"),
        cutoff=edition.cutoff.isoformat(),
        max_items=str(max_items),
        max_reserves=str(MAX_RESERVES),
        candidates_block="\n".join(_line(c) for c in cands),
    )


def _parse_picks(raw: list, by_key: dict[str, Candidate], used: set[str], *, reserve: bool) -> list[Pick]:
    picks = []
    for entry in raw or []:
        ids = [i for i in (entry.get("ids") or []) if isinstance(i, str)]
        unknown = [i for i in ids if i not in by_key]
        if unknown:
            raise LLMUnavailableError(f"editor chose unknown candidate ids {unknown}")
        if not ids or any(i in used for i in ids):
            raise LLMUnavailableError(f"editor reused or omitted candidate ids: {ids}")
        used.update(ids)
        group = sorted((by_key[i] for i in ids), key=lambda c: (-c.score, c.key))
        picks.append(Pick(group, str(entry.get("reason") or "")[:300], reserve=reserve))
    return picks


def editor_select(provider, cands: list[Candidate], edition, max_items: int = MAX_ITEMS) -> Selection:
    import time

    by_key = {c.key: c for c in cands}
    t0 = time.monotonic()
    data = provider._complete(build_select_prompt(cands, edition, max_items))
    used: set[str] = set()
    picks = _parse_picks(data.get("selected"), by_key, used, reserve=False)
    if len(picks) > max_items:
        raise LLMUnavailableError(f"editor chose {len(picks)} items, more than {max_items}")
    reserves = _parse_picks(data.get("reserves"), by_key, used, reserve=True)[:MAX_RESERVES]
    not_chosen = [
        {"id": n.get("id"), "title": by_key[n["id"]].title, "reason": str(n.get("reason") or "")[:200]}
        for n in data.get("not_chosen") or [] if isinstance(n, dict) and n.get("id") in by_key
    ]
    return Selection("editor_model", picks, reserves, not_chosen, model=getattr(provider, "model", None),
                     latency_ms=round((time.monotonic() - t0) * 1000))


def _similar(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= 0.6


def deterministic_select(cands: list[Candidate], max_items: int = MAX_ITEMS, error: str | None = None) -> Selection:
    """Pre-rank order; near-identical titles folded into one item; at most two items per bucket; demoted and
    previously-covered candidates skipped."""
    picks: list[Pick] = []
    per_bucket: dict[str, int] = {}
    for c in cands:
        if len(picks) >= max_items + MAX_RESERVES:
            break
        if c.demoted or c.previously or per_bucket.get(c.bucket, 0) >= 2:
            continue
        dup = next((p for p in picks if _similar(p.candidates[0].title, c.title)), None)
        if dup:
            dup.candidates.append(c)
            continue
        per_bucket[c.bucket] = per_bucket.get(c.bucket, 0) + 1
        picks.append(Pick([c], "deterministic pre-rank", reserve=len(picks) >= max_items))
    return Selection("deterministic_fallback", [p for p in picks if not p.reserve], [p for p in picks if p.reserve],
                     error=error)


def select(provider, cands: list[Candidate], edition, max_items: int = MAX_ITEMS) -> Selection:
    if not cands:
        return Selection("editor_model" if provider else "deterministic_fallback", [], [])
    if provider is None:
        return deterministic_select(cands, max_items, error="no provider configured")
    try:
        return editor_select(provider, cands, edition, max_items)
    except LLMUnavailableError as exc:
        return deterministic_select(cands, max_items, error=f"editor call failed: {exc}"[:300])
