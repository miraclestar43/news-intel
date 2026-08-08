"""Bounded Hermes editorial layer for the daily-canary content-quality
repair (2026-08-07: canary 2/3's purely-deterministic selection produced a
2-item, single-category, marketing-led digest -- see the root-cause
analysis in the incident report this module accompanies).

NOT wired into any delivery path. This module only: (1) builds a bounded,
deterministic, source-backed candidate packet from the DB, (2) calls
Hermes's already-validated one-shot CLI interface (`hermes -z ... -t ""`,
first proven out during Gate B's smoke test) to get a structured editorial
judgment, (3) strictly, deterministically validates that judgment before
trusting any of it, (4) renders the final bilingual Telegram-format message
using ONLY database-sourced URLs -- never a URL Hermes wrote. Hermes's
configuration/model/credentials are never touched; no other LLM provider is
added; `-t ""` disables all toolsets, so Hermes cannot browse or invent
sources -- it only ever sees the bounded packet it is given.

If Hermes is unavailable, times out, or returns anything that fails strict
validation, this module does NOT fall back to a padded/weak digest -- see
build_review_only_fallback, which produces a clearly-labeled, non-delivery
deterministic candidate list for human review instead."""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

ALLOWED_CATEGORIES = frozenset({
    "AI & Semiconductors", "Markets & Economy", "Politics", "US / China / World", "Technology",
})

MAX_CANDIDATES = 150
MIN_SELECTED = 4
MAX_SELECTED = 8
MAX_TOP3 = 3
MAX_MESSAGE_CHARS = 3800
HERMES_TIMEOUT_SECONDS = 240.0
HERMES_INTERFACE_LABEL = "hermes -z (oneshot CLI, -t \"\" no toolsets)"


@dataclass
class Candidate:
    event_id: str
    title: str
    excerpt: str
    updated_at: str | None
    category: str
    score: float
    source_name: str
    source_url: str

    def to_prompt_dict(self) -> dict:
        return {
            "event_id": self.event_id, "title": self.title, "excerpt": self.excerpt,
            "updated_at": self.updated_at, "category": self.category, "score": self.score,
            "source_name": self.source_name, "source_url": self.source_url,
        }


def build_candidate_packet(
    conn: sqlite3.Connection, *, window_start: datetime, window_end: datetime,
    max_candidates: int = MAX_CANDIDATES,
) -> list[Candidate]:
    """Deterministic, safety-scoped candidate pool: current (in-window),
    source-backed, not explicit noise, not archive-only. Deliberately does
    NOT pre-filter on score or exclude 'Unclassified' category -- Hermes's
    whole job is to judge significance and correct the miscategorization
    that the deterministic keyword classifier misses (2026-08-07 incident:
    73% of in-window events were force-classified 'Unclassified' with a
    hard-coded 0.0 relevance multiplier, discarding real markets/politics/
    China coverage regardless of substance)."""
    rows = conn.execute(
        """
        SELECT e.* FROM events e
        WHERE COALESCE(e.content_changed_at, e.published_anchor_at) >= ?
          AND COALESCE(e.content_changed_at, e.published_anchor_at) < ?
          AND EXISTS (SELECT 1 FROM event_sources es2 WHERE es2.event_id = e.id)
          AND (e.category IS NULL OR e.category != 'noise')
          AND (e.archive_only IS NULL OR e.archive_only = 0)
        ORDER BY e.final_score DESC
        LIMIT ?
        """,
        (window_start.astimezone(UTC).isoformat(), window_end.astimezone(UTC).isoformat(), max_candidates),
    ).fetchall()

    candidates: list[Candidate] = []
    for e in rows:
        src = conn.execute(
            """
            SELECT s.name AS source_name, r.canonical_url AS url, r.summary_excerpt AS excerpt
            FROM event_sources es JOIN raw_items r ON r.id = es.raw_item_id JOIN sources s ON s.id = r.source_id
            WHERE es.event_id = ? ORDER BY es.is_primary DESC, r.id LIMIT 1
            """,
            (e["id"],),
        ).fetchone()
        if src is None or not src["url"]:
            continue  # defense in depth; the EXISTS clause above should already prevent this
        candidates.append(Candidate(
            event_id=e["id"], title=e["primary_title"], excerpt=(src["excerpt"] or "")[:300],
            updated_at=e["content_changed_at"] or e["published_anchor_at"],
            category=e["category"] or "Unclassified", score=round(e["final_score"], 1),
            source_name=src["source_name"], source_url=src["url"],
        ))
    return candidates


def render_hermes_prompt(candidates: list[Candidate]) -> str:
    allowed = sorted(ALLOWED_CATEGORIES)
    schema_example = (
        '{\n  "selected": [\n'
        '    {"event_id": "...", "tier": "top3", "category": "...", '
        '"english_summary": "...", "why_it_matters": "...", "chinese_takeaway": "..."},\n'
        '    {"event_id": "...", "tier": "quick", "category": "...", '
        '"english_summary": "...", "chinese_takeaway": "..."}\n'
        "  ]\n}"
    )
    lines = [
        "You are a strict, careful news editor for a daily intelligence brief. You will be given a "
        'JSON array of candidate news items ("candidates") from the last 24 hours. Each candidate has: '
        "event_id, title, excerpt, updated_at, category (a possibly-wrong existing classification), "
        "score (a possibly-unreliable deterministic score), source_name, source_url.",
        "",
        "Your job:",
        f"1. Select {MIN_SELECTED} to {MAX_SELECTED} candidates that are genuinely significant "
        "intelligence-worthy developments for a reader tracking AI/semiconductors, markets/"
        "macroeconomics, US politics/policy, and geopolitics/US-China relations.",
        "2. REJECT marketing, customer-story, case-study, or promotional content -- even if it comes "
        'from an "official" source. A vendor blog post about how "Company X uses Product Y to improve '
        'productivity" is NOT news, regardless of score.',
        "3. Do not make AI the mandatory majority category. Prefer a genuine mix across the allowed "
        "categories below -- but ONLY include categories that actually have qualified stories. Do not "
        "pad with weak items to hit a category quota.",
        "4. You may correct an item's category if the existing one is wrong (the existing classifier "
        'is known to be unreliable and over-uses "Unclassified"). Only use one of these category '
        f"values: {allowed}.",
        f'5. Pick the best 1 to {MAX_TOP3} of your selections as "top3" (the most significant), and the '
        'rest as "quick" (worth a one-line mention).',
        "6. For each selected item, write:",
        '   - "english_summary": for tier=top3, one or two concise sentences describing what happened, '
        "using ONLY facts present in the title/excerpt given -- never invent numbers, quotes, or facts. "
        "For tier=quick, a short single clause.",
        '   - "why_it_matters": (top3 only) one concise sentence on significance.',
        '   - "chinese_takeaway": one concise Chinese sentence (Simplified Chinese) summarizing the item.',
        "7. You MUST only use event_id values that appear in the candidates below. Never invent an "
        "event_id. Never include a URL or source name in your output -- the system attaches the "
        "correct source itself.",
        "",
        "Respond with ONLY a single JSON object (no markdown fences, no commentary) matching exactly "
        "this shape:",
        schema_example,
        "",
        "CANDIDATES:",
        json.dumps([c.to_prompt_dict() for c in candidates], ensure_ascii=False),
    ]
    return "\n".join(lines)


@dataclass
class HermesCallResult:
    raw_text: str
    latency_seconds: float
    returncode: int
    timed_out: bool
    error: str | None = None


def call_hermes(prompt: str, *, timeout_seconds: float = HERMES_TIMEOUT_SECONDS) -> HermesCallResult:
    """The already-validated non-interactive interface: `hermes -z <prompt>
    -t ""` (Gate B's smoke test: one-shot mode, empty toolset so no tool-
    calling/browsing is possible, auto-bypassed approvals, prints only the
    final response text). Does not touch Hermes config/model/credentials --
    no --model/--provider override, no config file writes."""
    start = time.monotonic()
    try:
        proc = subprocess.run(
            ["hermes", "-z", prompt, "-t", ""],
            capture_output=True, text=True, timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired:
        return HermesCallResult("", time.monotonic() - start, -1, True, "timed out")
    except FileNotFoundError as exc:
        return HermesCallResult("", time.monotonic() - start, -1, False, f"hermes CLI not found: {exc}")

    latency = time.monotonic() - start
    if proc.returncode != 0:
        err = proc.stderr.strip() or "non-zero exit"
        return HermesCallResult(proc.stdout or "", latency, proc.returncode, False, err)
    return HermesCallResult(proc.stdout, latency, proc.returncode, False, None)


class HermesSelectedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str
    tier: Literal["top3", "quick"]
    category: str
    english_summary: str = Field(min_length=1, max_length=500)
    why_it_matters: str | None = Field(default=None, max_length=300)
    chinese_takeaway: str = Field(min_length=1, max_length=200)

    @field_validator("category")
    @classmethod
    def _valid_category(cls, v: str) -> str:
        if v not in ALLOWED_CATEGORIES:
            raise ValueError(f"category {v!r} is not one of the allowed categories {sorted(ALLOWED_CATEGORIES)}")
        return v


class HermesEditorialResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected: list[HermesSelectedItem]


@dataclass
class ResolvedItem:
    """A Hermes-selected item after the deterministic safety envelope has
    resolved it back against the database -- category/summary/why-it-
    matters/chinese_takeaway come from Hermes (validated); title/URL/source
    come from the DB, never from Hermes."""
    event_id: str
    tier: Literal["top3", "quick"]
    category: str
    english_summary: str
    why_it_matters: str | None
    chinese_takeaway: str
    title: str
    source_url: str
    source_name: str


@dataclass
class ValidationOutcome:
    ok: bool
    items: list[ResolvedItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _strip_code_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    return stripped.strip()


def validate_hermes_response(raw_text: str, candidates: list[Candidate]) -> ValidationOutcome:
    """Every check the 2026-08-07 remediation requires before Hermes's
    output can be trusted at all: valid JSON against the strict schema,
    every event_id present in the supplied candidate set (never invented),
    no duplicates, count within [MIN_SELECTED, MAX_SELECTED], at most
    MAX_TOP3 top3 items, category corrected to an allowed value (Hermes
    literally cannot emit "Unclassified" -- it isn't in the allowed set),
    and URL/title always resolved from the DB-backed candidate, never from
    Hermes's own text."""
    candidate_by_id = {c.event_id: c for c in candidates}

    cleaned = _strip_code_fences(raw_text)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        return ValidationOutcome(False, errors=[f"Hermes output is not valid JSON: {exc}"])

    try:
        parsed = HermesEditorialResponse.model_validate(payload)
    except ValidationError as exc:
        return ValidationOutcome(False, errors=[f"Hermes output failed schema validation: {exc}"])

    errors: list[str] = []
    seen_ids: set[str] = set()
    resolved: list[ResolvedItem] = []
    top3_count = 0

    for item in parsed.selected:
        if item.event_id not in candidate_by_id:
            errors.append(f"event_id {item.event_id!r} is not in the supplied candidate set")
            continue
        if item.event_id in seen_ids:
            errors.append(f"duplicate event_id {item.event_id!r}")
            continue
        seen_ids.add(item.event_id)

        if item.tier == "top3":
            top3_count += 1
            if not item.why_it_matters:
                errors.append(f"event_id {item.event_id!r} is tier=top3 but has no why_it_matters")

        candidate = candidate_by_id[item.event_id]
        resolved.append(ResolvedItem(
            event_id=item.event_id, tier=item.tier, category=item.category,
            english_summary=item.english_summary, why_it_matters=item.why_it_matters,
            chinese_takeaway=item.chinese_takeaway, title=candidate.title,
            source_url=candidate.source_url, source_name=candidate.source_name,
        ))

    if top3_count > MAX_TOP3:
        errors.append(f"{top3_count} top3 items exceeds the max of {MAX_TOP3}")
    if not (MIN_SELECTED <= len(resolved) <= MAX_SELECTED):
        errors.append(f"{len(resolved)} selected items is outside the required [{MIN_SELECTED}, {MAX_SELECTED}] range")

    if errors:
        return ValidationOutcome(False, items=resolved, errors=errors)
    return ValidationOutcome(True, items=resolved, errors=[])


def render_hermes_digest(date_label: str, items: list[ResolvedItem]) -> str:
    """Exact format from the 2026-08-07 content-quality remediation spec:
    TOP 3 gets a full block (headline/summary/why-it-matters/Chinese/
    source); QUICK BRIEFS gets one line each. English-first, one Chinese
    sentence per item -- never a duplicate full translation of the digest."""
    top3 = [i for i in items if i.tier == "top3"]
    quick = [i for i in items if i.tier == "quick"]

    lines = [f"Daily Intelligence Brief — {date_label}", ""]
    if top3:
        lines.append("TOP 3")
        for idx, item in enumerate(top3, start=1):
            lines.append("")
            lines.append(f"{idx}. {item.title}")
            lines.append(f"English summary: {item.english_summary}")
            lines.append(f"Why it matters: {item.why_it_matters}")
            lines.append(f"中文要点：{item.chinese_takeaway}")
            lines.append(f"Source: {item.source_url}")
    if quick:
        lines.append("")
        lines.append("QUICK BRIEFS")
        for item in quick:
            lines.append(f"- {item.title} — {item.chinese_takeaway} {item.source_url}")
    return "\n".join(lines)


def build_review_only_fallback(date_label: str, candidates: list[Candidate], errors: list[str]) -> str:
    """When Hermes is unavailable or its output fails validation: never
    send a padded/weak digest. This is explicitly NOT a delivery-formatted
    message -- it lists the top deterministic candidates for a human to
    review, clearly labeled so it can never be mistaken for a real brief."""
    lines = [
        f"[REVIEW ONLY -- Hermes-assisted digest NOT generated for {date_label}]",
        "",
        "Reason(s):",
        *[f"- {e}" for e in errors],
        "",
        "Top deterministic candidates for manual review (not a digest, not for delivery):",
    ]
    for c in candidates[:15]:
        lines.append(f"- [{c.category}] score={c.score} {c.title} ({c.source_url})")
    return "\n".join(lines)


@dataclass
class HermesEditorialOutcome:
    fallback_used: bool
    rendered_text: str
    interface: str
    model_used: str
    latency_seconds: float
    validation_errors: list[str]
    raw_hermes_text: str | None
    candidate_count: int
    selected_count: int
    character_count: int


def compose_hermes_assisted_digest(
    conn: sqlite3.Connection, *, window_start: datetime, window_end: datetime, date_label: str,
    max_candidates: int = MAX_CANDIDATES, timeout_seconds: float = HERMES_TIMEOUT_SECONDS,
) -> HermesEditorialOutcome:
    candidates = build_candidate_packet(
        conn, window_start=window_start, window_end=window_end, max_candidates=max_candidates
    )

    if not candidates:
        no_candidates_reason = "no source-backed, non-noise candidates in window"
        text = build_review_only_fallback(date_label, [], [no_candidates_reason])
        return HermesEditorialOutcome(
            True, text, HERMES_INTERFACE_LABEL, "none", 0.0, [no_candidates_reason], None, 0, 0, len(text)
        )

    prompt = render_hermes_prompt(candidates)
    call = call_hermes(prompt, timeout_seconds=timeout_seconds)

    if call.error is not None:
        text = build_review_only_fallback(date_label, candidates, [f"Hermes call failed: {call.error}"])
        return HermesEditorialOutcome(
            True, text, HERMES_INTERFACE_LABEL, "unavailable", call.latency_seconds,
            [f"Hermes call failed: {call.error}"], call.raw_text or None, len(candidates), 0, len(text),
        )

    outcome = validate_hermes_response(call.raw_text, candidates)
    if not outcome.ok:
        text = build_review_only_fallback(date_label, candidates, outcome.errors)
        return HermesEditorialOutcome(
            True, text, HERMES_INTERFACE_LABEL, "mimo-v2.5 (invalid output)", call.latency_seconds,
            outcome.errors, call.raw_text, len(candidates), 0, len(text),
        )

    rendered = render_hermes_digest(date_label, outcome.items)
    if len(rendered) > MAX_MESSAGE_CHARS:
        text = build_review_only_fallback(
            date_label, candidates, [f"rendered digest is {len(rendered)} chars, exceeds {MAX_MESSAGE_CHARS}"]
        )
        return HermesEditorialOutcome(
            True, text, HERMES_INTERFACE_LABEL, "mimo-v2.5 (over length)", call.latency_seconds,
            [f"rendered digest is {len(rendered)} chars, exceeds {MAX_MESSAGE_CHARS}"], call.raw_text,
            len(candidates), len(outcome.items), len(text),
        )

    return HermesEditorialOutcome(
        False, rendered, HERMES_INTERFACE_LABEL, "mimo-v2.5", call.latency_seconds, [], call.raw_text,
        len(candidates), len(outcome.items), len(rendered),
    )
