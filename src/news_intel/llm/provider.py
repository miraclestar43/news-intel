from __future__ import annotations

from news_intel.llm.openai_compatible import OpenAICompatibleProvider
from news_intel.settings import AppConfig


class NullProvider:
    """Used when no LLM_BASE_URL/LLM_API_KEY/LLM_MODEL is configured. Every
    method raises so callers take the documented deterministic-fallback path
    rather than silently pretending to have called a model."""

    def _unconfigured(self):
        from news_intel.llm.errors import LLMUnavailableError

        raise LLMUnavailableError(
            "no LLM provider configured: set LLM_BASE_URL, LLM_API_KEY, LLM_MODEL"
        )

    def enrich_events(self, events):
        self._unconfigured()

    def compose_daily(self, inputs):
        self._unconfigured()

    def compose_weekly(self, inputs):
        self._unconfigured()

    def explain_event(self, event):
        self._unconfigured()


def get_provider(config: AppConfig):
    secrets = config.secrets
    if secrets.llm_provider == "hermes_cli":
        if not secrets.hermes_bin:
            return NullProvider()
        from news_intel.llm.hermes_cli import HermesCliProvider

        return HermesCliProvider(
            bin_path=secrets.hermes_bin,
            timeout=secrets.hermes_timeout_seconds or 20.0,
            model_label=secrets.hermes_model_label,
        )
    if secrets.llm_base_url and secrets.llm_api_key and secrets.llm_model:
        return OpenAICompatibleProvider(
            base_url=secrets.llm_base_url,
            api_key=secrets.llm_api_key,
            model=secrets.llm_model,
        )
    return NullProvider()
