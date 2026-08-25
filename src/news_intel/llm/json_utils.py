"""Shared response-parsing helpers for LLMProvider implementations
(2026-08-24): every provider returns raw model text that may be wrapped in
markdown code fences despite the system prompt's explicit instruction not
to -- strip those before json.loads, consistently, in one place."""

from __future__ import annotations

import json


def strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


def parse_json_object(text: str) -> dict:
    return json.loads(strip_fences(text))
