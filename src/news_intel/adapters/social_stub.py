from __future__ import annotations

from news_intel.adapters.base import FetchOutcome, SourceHealthState, register


@register("social_stub")
class SocialStubAdapter:
    """OptionalSocialAdapter interface for requested X handles.

    No compliant free public endpoint has been validated for these handles
    (X's official API is paid; public pages require login and prohibit
    scraping/circumvention). Rather than pretend to monitor them, this
    adapter reports 'unavailable' with a clear reason. If a compliant
    read-only endpoint is later identified, implement fetch() here without
    touching login cookies, account rotation, or anti-bot bypasses.
    """

    def fetch(self, source, cursor: str | None) -> FetchOutcome:
        detail = source.note if getattr(source, "note", None) else (
            "no compliant free public endpoint validated; T3 discovery only, "
            "never a core dependency"
        )
        return FetchOutcome([], cursor, SourceHealthState.UNAVAILABLE, detail)
