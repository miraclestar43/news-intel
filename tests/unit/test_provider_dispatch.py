"""2026-08-24: provider-mode dispatch (LLM_PROVIDER=hermes_cli vs the
LLM_BASE_URL/LLM_API_KEY/LLM_MODEL HTTP path vs unconfigured) for both
llm.provider.get_provider (whole-digest path) and pipeline.
editorial_enrichment.build_bounded_provider (per-story path). Uses the
`config` fixture, which guarantees a clean LLM_*/HERMES_* environment, and
mutates config.secrets directly -- never sets real env vars or touches any
real Hermes/OpenAI-compatible endpoint."""

from __future__ import annotations

from news_intel.llm.hermes_cli import HermesCliProvider
from news_intel.llm.openai_compatible import OpenAICompatibleProvider
from news_intel.llm.provider import NullProvider, get_provider
from news_intel.pipeline.editorial_enrichment import build_bounded_provider


def _with_secrets(config, **updates):
    return config.model_copy(update={"secrets": config.secrets.model_copy(update=updates)})


def test_get_provider_returns_null_when_unconfigured(config):
    assert isinstance(get_provider(config), NullProvider)


def test_get_provider_returns_openai_compatible_when_http_configured(config):
    cfg = _with_secrets(config, llm_base_url="https://example.test/v1", llm_api_key="k", llm_model="m")
    assert isinstance(get_provider(cfg), OpenAICompatibleProvider)


def test_get_provider_returns_hermes_cli_when_provider_mode_set(config):
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes")
    provider = get_provider(cfg)
    assert isinstance(provider, HermesCliProvider)
    assert provider.bin_path == "/usr/local/bin/hermes"


def test_get_provider_hermes_cli_mode_without_bin_returns_null(config):
    """LLM_PROVIDER=hermes_cli with no HERMES_BIN must never silently fall
    through to the HTTP path or crash -- it's simply unconfigured."""
    cfg = _with_secrets(config, llm_provider="hermes_cli", llm_base_url="https://example.test/v1",
                         llm_api_key="k", llm_model="m")
    assert isinstance(get_provider(cfg), NullProvider)


def test_get_provider_hermes_cli_mode_ignores_http_vars_when_bin_present(config):
    """hermes_cli mode takes priority even if LLM_BASE_URL/etc happen to
    also be set -- explicit provider selection, never an ambiguous mix."""
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes",
                         llm_base_url="https://example.test/v1", llm_api_key="k", llm_model="m")
    assert isinstance(get_provider(cfg), HermesCliProvider)


def test_get_provider_hermes_cli_uses_configured_timeout_and_label(config):
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes",
                         hermes_timeout_seconds=45.0, hermes_model_label="mimo-v2.5")
    provider = get_provider(cfg)
    assert provider.timeout == 45.0
    assert provider.model == "mimo-v2.5"


def test_build_bounded_provider_none_when_unconfigured(config):
    assert build_bounded_provider(config) is None


def test_build_bounded_provider_hermes_cli(config):
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes")
    provider = build_bounded_provider(cfg)
    assert isinstance(provider, HermesCliProvider)


def test_build_bounded_provider_hermes_cli_caps_timeout_to_story_budget(config):
    """The per-story call must stay bounded by the stricter story timeout
    even if HERMES_TIMEOUT_SECONDS is configured larger for other uses.
    The hermes_cli ceiling (60s) is looser than the HTTP path's (12s) --
    it must absorb fixed CLI subprocess/interpreter startup overhead plus
    real observed per-story latency the HTTP path doesn't pay."""
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes",
                         hermes_timeout_seconds=999.0)
    provider = build_bounded_provider(cfg)
    assert provider.timeout <= 60.0


def test_build_bounded_provider_hermes_cli_without_bin_returns_none(config):
    cfg = _with_secrets(config, llm_provider="hermes_cli")
    assert build_bounded_provider(cfg) is None


def test_build_bounded_provider_openai_compatible(config):
    cfg = _with_secrets(config, llm_base_url="https://example.test/v1", llm_api_key="k", llm_model="m")
    provider = build_bounded_provider(cfg)
    assert isinstance(provider, OpenAICompatibleProvider)


def test_secrets_repr_never_leaks_hermes_bin_or_timeout(config):
    """Secrets.__repr__ is already hardened to redact everything -- prove
    the new fields don't accidentally bypass that via a custom __repr__ on
    a subclass or field-level override (there is none; this just pins the
    existing global redaction to cover the new fields too)."""
    cfg = _with_secrets(config, llm_provider="hermes_cli", hermes_bin="/usr/local/bin/hermes",
                         hermes_model_label="mimo-v2.5")
    assert repr(cfg.secrets) == "Secrets(<redacted>)"
    assert str(cfg.secrets) == "Secrets(<redacted>)"
