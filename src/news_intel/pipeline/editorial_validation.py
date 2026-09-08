"""Deterministic post-generation validators for the optional per-story LLM
editorial layer (2026-08-24). An LLM's structured output is a PROPOSAL,
never trusted directly -- every claim is checked against the same evidence
packet (pipeline.editorial_evidence.build_event_input) that was offered to
the model before the output is allowed to reach a digest. A validation
failure never raises; callers get a list of human-readable reasons and
decide whether to retry-with-feedback or fall back.
"""

from __future__ import annotations

import re

from news_intel.models import EventEnrichment, EventInput
from news_intel.pipeline.entity_aliases import alias_variants
from news_intel.pipeline.summary_quality import _ends_with_dangling_clause, _is_abbreviation_period
from news_intel.pipeline.zh_text import parse_zh_amounts

# 2026-08-24: the exact banned patterns named across this incident's
# multiple review rounds -- checked against EVERY text field the model
# returns, not just why_it_matters, so a banned phrase can't slip through
# via chinese_takeaway or what_happened instead.
_BANNED_PATTERNS = [
    re.compile(r"ranks among today'?s? most material", re.IGNORECASE),
    re.compile(r"this development involving", re.IGNORECASE),
    re.compile(r"a material development in", re.IGNORECASE),
    re.compile(r"属.{0,20}重要动态$"),
    re.compile(r"newly named \("),
    re.compile(r"^worth \$", re.IGNORECASE),  # "worth $X" as the ENTIRE clause -- a bare, roleless amount
    # 2026-09-07 production incident (Sep 5 en_global, DELIVERED): the
    # Insider Monkey item's "why it matters" was "This trend could indicate
    # that investor interest ... potentially affecting valuations across
    # chip manufacturing equipment and materials companies." That is a
    # tautology -- any market story "could affect valuations" -- not a
    # story-specific consequence. Generic hedged market-implication
    # boilerplate is rejected the same way the category-label templates are.
    re.compile(r"could (?:affect|impact|influence) (?:valuations|share prices|stock prices|sentiment)",
               re.IGNORECASE),
    re.compile(r"potentially affecting valuations", re.IGNORECASE),
    re.compile(r"may (?:affect|impact) (?:valuations|investor sentiment)", re.IGNORECASE),
    re.compile(r"或将?影响.{0,10}估值"),
    re.compile(r"可能会?影响.{0,10}估值"),
]

_NUMBER_RE = re.compile(r"[\d,]+(?:\.\d+)?")


_ZH_MAGNITUDE_SUFFIXES = ("万亿", "亿元", "亿美元", "亿", "万元", "万美元", "万")

# 2026-08-25 repair: real live-model replay found a genuine packaging gap,
# not a model hallucination -- a raw source excerpt said "710M shares"; the
# model (correctly) wrote the number out in full as "710,000,000", but
# _numbers_in() scanning that SAME raw excerpt for evidence only ever
# extracted the bare "710" (no magnitude handling for English M/B/T
# suffixes, unlike the 万/亿 handling already here for Chinese), so a
# real, traceable figure was rejected as unsupported. Mirrors the existing
# Chinese magnitude-suffix pattern: skip the bare digit match and
# contribute only the correctly scaled value.
_EN_MAGNITUDE_WORD_RE = re.compile(r"^\s?(million|billion|trillion|mn|bn|tn)\b", re.IGNORECASE)
_EN_MAGNITUDE_LETTER_RE = re.compile(r"^(M|B|T)\b")
_EN_MAGNITUDE_MULTIPLIERS = {
    "million": 1_000_000, "mn": 1_000_000, "m": 1_000_000,
    "billion": 1_000_000_000, "bn": 1_000_000_000, "b": 1_000_000_000,
    "trillion": 1_000_000_000_000, "tn": 1_000_000_000_000, "t": 1_000_000_000_000,
}


def _numbers_in(text: str) -> set[float]:
    """Both plain numbers (English/bare figures) and Chinese-magnitude
    figures (2.5亿 -> 250,000,000) -- a Chinese takeaway field routinely
    expresses the SAME evidence number using 万/亿 notation, which must
    resolve to the same underlying value as the English fields' plain
    digits, not be flagged as a second, untraceable number. A bare digit
    match immediately followed by a magnitude suffix (Chinese 万/亿 or
    English million/billion/trillion/M/B/T) is skipped in its bare form
    and contributes only the correctly scaled value -- so "2.5亿" or
    "710M" never ALSO contributes a second, wrong bare 2.5/710."""
    text = text or ""
    found = set()
    for match in _NUMBER_RE.finditer(text):
        tail = text[match.end() : match.end() + 4]
        if any(tail.startswith(suffix) for suffix in _ZH_MAGNITUDE_SUFFIXES):
            continue
        raw = match.group(0).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        remainder = text[match.end() : match.end() + 10]
        word_match = _EN_MAGNITUDE_WORD_RE.match(remainder)
        letter_match = None if word_match else _EN_MAGNITUDE_LETTER_RE.match(remainder)
        if word_match:
            value *= _EN_MAGNITUDE_MULTIPLIERS[word_match.group(1).lower()]
        elif letter_match:
            value *= _EN_MAGNITUDE_MULTIPLIERS[letter_match.group(1).lower()]
        found.add(value)
    found |= set(parse_zh_amounts(text))
    return found


class ValidationResult:
    def __init__(self, ok: bool, reasons: list[str]):
        self.ok = ok
        self.reasons = reasons

    def __bool__(self) -> bool:
        return self.ok


def validate_enrichment(enrichment: EventEnrichment, evidence: EventInput) -> ValidationResult:
    reasons: list[str] = []

    text_fields = [enrichment.what_happened, enrichment.why_it_matters, enrichment.chinese_what_happened,
                   enrichment.chinese_takeaway, enrichment.chinese_headline, enrichment.english_headline]

    # 1. Banned generic language, in every text field.
    for field_name, value in (
        ("what_happened", enrichment.what_happened), ("why_it_matters", enrichment.why_it_matters),
        ("chinese_what_happened", enrichment.chinese_what_happened),
        ("chinese_takeaway", enrichment.chinese_takeaway),
    ):
        for pattern in _BANNED_PATTERNS:
            if pattern.search(value or ""):
                reasons.append(f"{field_name} contains banned generic phrase: {pattern.pattern!r}")

    # 2. cited_fact_ids: must be non-empty (unsupported) and a subset of the
    # ids actually offered (fabricated fact reference).
    offered_ids = {f.id for f in evidence.facts}
    if not enrichment.cited_fact_ids:
        reasons.append("cited_fact_ids is empty -- output is not traceable to any offered evidence")
    else:
        fabricated = set(enrichment.cited_fact_ids) - offered_ids
        if fabricated:
            reasons.append(f"cited_fact_ids references facts never offered: {sorted(fabricated)}")

    # 3. Numeric evidence: every number appearing in the reader-facing text
    # must match (within 1% relative tolerance, for formatting/rounding)
    # a number present in the offered facts OR the raw source excerpts --
    # never a number the model introduced from nowhere.
    evidence_numbers = {f.value for f in evidence.facts if f.value is not None}
    for s in evidence.sources:
        evidence_numbers |= _numbers_in(s.get("excerpt", ""))
    output_numbers = set()
    for value in text_fields:
        output_numbers |= _numbers_in(value)
    for n in output_numbers:
        if n <= 1:  # percentages, small counts, dates-as-numbers -- too noisy to police at this granularity
            continue
        if not any(abs(n - e) / max(e, 1) < 0.01 for e in evidence_numbers):
            reasons.append(f"output contains a number ({n}) not traceable to any offered evidence or source text")

    # 4. Actor/target: any known-entity name the model asserts in entities/
    # material_facts should be traceable to the offered actor/target facts
    # or the candidate_title/source excerpts -- never a wholly invented
    # name. Case-insensitive: a real live-model replay showed "NVIDIA" (the
    # model's own capitalization) rejected against an offered "Nvidia" --
    # a capitalization variance, not a fabricated entity, and a strict
    # case match rejects real, traceable names for no safety benefit.
    # Alias-aware (2026-08-25): a real live-model replay showed a Chinese
    # source excerpt naming banks in Chinese ("高盛、摩根士丹利...") get
    # correctly summarized in English ("Goldman Sachs, Morgan Stanley...")
    # and rejected as untraceable purely because the two scripts don't
    # literally substring-match -- the same entity in a different script
    # is not a fabrication. pipeline.entity_aliases is a small, curated,
    # general (not headline-specific) lookup for exactly this.
    known_actor_texts_lower = {f.text.lower() for f in evidence.facts if f.kind in ("actor", "target")}
    source_text_lower = " ".join(
        s.get("excerpt", "") + " " + evidence.candidate_title for s in evidence.sources
    ).lower()
    for name in enrichment.entities:
        variants = alias_variants(name)
        if any(v in known_actor_texts_lower or v in source_text_lower for v in variants):
            continue
        reasons.append(f"entity {name!r} is not traceable to any offered evidence or source text")

    # 5. Uncertainty preservation: if the evidence packet flagged prospective/
    # unconfirmed language, the output must not present the claim as flatly
    # settled -- require at least one hedge word survive into why_it_matters
    # or chinese_takeaway when the packet's uncertainty fact is present.
    has_uncertainty_fact = any(f.kind == "uncertainty" for f in evidence.facts)
    if has_uncertainty_fact:
        hedge_words = ("could", "might", "may", "reportedly", "discussed", "considering",
                       "或将", "据报道", "消息称", "据悉")
        combined_output = f"{enrichment.why_it_matters} {enrichment.chinese_takeaway}"
        if not any(h in combined_output for h in hedge_words):
            reasons.append(
                "evidence packet flagged prospective/unconfirmed language, but output presents it as settled fact"
            )

    # 6. Category consistency: must be one of the offered candidates (the
    # deterministic classifier's own decision), never a wild swap.
    if evidence.category_candidates and enrichment.category not in evidence.category_candidates:
        reasons.append(
            f"category {enrichment.category!r} is not one of the deterministic candidates "
            f"{evidence.category_candidates}"
        )

    # 7. Complete-sentence check (reuses the same dangling-clause/
    # abbreviation-period logic the deterministic renderer's own render-
    # readiness gate uses).
    sentence_fields = (("what_happened", enrichment.what_happened), ("why_it_matters", enrichment.why_it_matters),
                       ("chinese_what_happened", enrichment.chinese_what_happened))
    for field_name, value in sentence_fields:
        if not value or not value.strip():
            continue
        stripped = value.strip()
        if _ends_with_dangling_clause(stripped):
            reasons.append(f"{field_name} ends with a dangling/incomplete clause")
        tail = stripped[-4:-1] if len(stripped) > 3 else ""
        if stripped[-1] not in ".!?。！？" and not _is_abbreviation_period(tail):
            reasons.append(f"{field_name} does not end with terminal punctuation")

    # 8. URL / source-attribution integrity: supporting_source_ids must be a
    # non-empty subset of the offered source ids.
    offered_source_ids = {s["id"] for s in evidence.sources}
    if not enrichment.supporting_source_ids:
        reasons.append("supporting_source_ids is empty")
    else:
        bad_sources = set(enrichment.supporting_source_ids) - offered_source_ids
        if bad_sources:
            reasons.append(f"supporting_source_ids references sources never offered: {sorted(bad_sources)}")

    return ValidationResult(ok=not reasons, reasons=reasons)
