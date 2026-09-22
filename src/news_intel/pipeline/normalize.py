"""Stage 2: normalize a collected item into the bounded, hashable form stored
in raw_items. No dedupe/clustering decisions happen here."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from news_intel.adapters.base import CollectedItem
from news_intel.pipeline.text import canonical_text

_TRACKING_PARAM_PREFIXES = ("utm_", "ref", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid")

# Known aggregator redirect hosts whose only job is to bounce to the real
# article; strip the wrapper here so two aggregator copies of one URL match.
_AGGREGATOR_REDIRECT_HOSTS = {"news.google.com", "t.co"}


def canonicalize_url(raw_url: str) -> str:
    parsed = urlparse(raw_url.strip())
    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=True)
        if not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    query_pairs.sort()
    scheme = parsed.scheme.lower() or "https"
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/") or "/"
    new_query = urlencode(query_pairs)
    return urlunparse((scheme, netloc, path, "", new_query, ""))


def url_hash(canonical_url: str) -> str:
    return hashlib.sha256(canonical_url.encode("utf-8")).hexdigest()


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def normalize_title(title: str) -> str:
    lowered = title.strip().lower()
    no_punct = _PUNCT_RE.sub(" ", lowered)
    return _WS_RE.sub(" ", no_punct).strip()


def content_hash(normalized_title: str, excerpt: str | None) -> str:
    payload = normalized_title + "|" + (excerpt or "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class NormalizedItem:
    __slots__ = (
        "source_id",
        "external_id",
        "canonical_url",
        "url_hash",
        "title",
        "normalized_title",
        "summary_excerpt",
        "language",
        "published_at",
        "fetched_at",
        "content_hash",
        "origin_family",
        "origin_url",
        "origin_title",
        "origin_published_at",
        "edition_id",
        "metadata",
    )

    def __init__(self, **kwargs):
        for slot in self.__slots__:
            setattr(self, slot, kwargs[slot])


def normalize(
    item: CollectedItem, *, excerpt_max_chars: int, now: datetime | None = None, edition_id: str = "en_global"
) -> NormalizedItem:
    canonical = canonicalize_url(item.canonical_url)
    # canonical plain Unicode (pipeline/text.py): markup removed, entities
    # decoded exactly once -- before hashing, so content_hash reflects text
    title = canonical_text(item.title) or ""
    norm_title = normalize_title(title)
    excerpt = (canonical_text(item.summary_excerpt) or "")[:excerpt_max_chars] or None
    return NormalizedItem(
        source_id=item.source_id,
        external_id=item.external_id,
        canonical_url=canonical,
        url_hash=url_hash(canonical),
        title=title,
        normalized_title=norm_title,
        summary_excerpt=excerpt,
        language=item.language,
        published_at=item.published_at,
        fetched_at=now or datetime.now(UTC),
        content_hash=content_hash(norm_title, excerpt),
        origin_family=item.origin_family,
        origin_url=item.origin_url,
        origin_title=canonical_text(item.origin_title),
        origin_published_at=item.origin_published_at,
        edition_id=edition_id,
        metadata=item.metadata,
    )
