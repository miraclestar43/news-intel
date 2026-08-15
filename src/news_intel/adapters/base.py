"""Adapter protocol: one class per source type, each isolated so one broken
feed or selector degrades only that source, never the whole collection job."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SourceHealthState(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"


@dataclass
class CollectedItem:
    source_id: str
    external_id: str | None
    canonical_url: str
    title: str
    summary_excerpt: str | None
    language: str
    published_at: datetime | None
    origin_family: str
    origin_url: str | None = None
    metadata: dict = field(default_factory=dict)


@dataclass
class FetchOutcome:
    items: list[CollectedItem]
    new_cursor: str | None
    health_state: SourceHealthState
    health_detail: str | None = None
    retry_after_seconds: float | None = None


class SourceAdapter(Protocol):
    def fetch(self, source, cursor: str | None) -> FetchOutcome: ...


_REGISTRY: dict[str, type] = {}


def register(adapter_type: str):
    def _wrap(cls):
        _REGISTRY[adapter_type] = cls
        return cls

    return _wrap


def get_adapter(adapter_type: str) -> SourceAdapter:
    # imported lazily by adapters/__init__ to populate _REGISTRY before use
    if adapter_type not in _REGISTRY:
        raise KeyError(f"no adapter registered for type {adapter_type!r}")
    return _REGISTRY[adapter_type]()
