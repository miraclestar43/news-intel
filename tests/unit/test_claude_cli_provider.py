"""2026-11-01: ClaudeCliProvider (LLM_PROVIDER=claude_cli). subprocess.run is monkeypatched -- never the real CLI.
Proves: argv list / shell=False / prompt on stdin / tools off; the child env never carries API keys; the model label is
the one the CLI reported; API-key auth, tools or an error result -> LLMUnavailableError (caller takes the floor);
dispatch from pi-news-intel's own settings only."""

from __future__ import annotations

import json
import subprocess

import pytest

from news_intel.llm.claude_cli import ClaudeCliProvider
from news_intel.llm.errors import LLMUnavailableError
from tests.unit.test_hermes_cli import GOOD_JSON, _evidence


def _stream(result_text=GOOD_JSON, *, model="claude-opus-5-5", api_key_source="none", tools=(), is_error=False):
    init = {
        "type": "system",
        "subtype": "init",
        "model": model,
        "claude_code_version": "2.1.308",
        "apiKeySource": api_key_source,
        "tools": list(tools),
        "mcp_servers": [],
    }
    result = {"type": "result", "subtype": "success", "is_error": is_error, "result": result_text, "duration_ms": 900}
    return json.dumps(init) + "\n" + json.dumps(result) + "\n"


class _P:
    def __init__(self, stdout, returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def _provider():
    return ClaudeCliProvider(bin_path="/home/x/.local/bin/claude", config_dir="/home/x/.config/c", timeout=9.0)


def test_invocation_shape_env_and_reported_model(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return _P(_stream())

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-never-pass")
    monkeypatch.setattr(subprocess, "run", fake_run)
    p = _provider()
    out = p.enrich_events([_evidence()])
    assert out[0].event_id == "E-1"
    assert seen["shell"] is False and seen["timeout"] == 9.0 and "Starcloud" in seen["input"]
    assert seen["argv"][0] == "/home/x/.local/bin/claude" and seen["argv"][seen["argv"].index("--tools") + 1] == ""
    assert "--bare" not in seen["argv"]
    assert "ANTHROPIC_API_KEY" not in seen["env"] and seen["env"]["CLAUDE_CONFIG_DIR"] == "/home/x/.config/c"
    assert p.model == "claude-opus-5-5 (Claude Code CLI 2.1.308)"


@pytest.mark.parametrize(
    "kw,msg",
    [
        ({"api_key_source": "ANTHROPIC_API_KEY"}, "subscription"),
        ({"tools": ["Bash"]}, "isolation"),
        ({"is_error": True, "result_text": "Claude AI usage limit reached"}, "usage limit"),
    ],
)
def test_fail_closed(monkeypatch, kw, msg):
    monkeypatch.setattr(subprocess, "run", lambda argv, **k: _P(_stream(**kw)))
    with pytest.raises(LLMUnavailableError, match=msg):
        _provider().enrich_events([_evidence()])


def test_timeout_and_missing_binary(monkeypatch):
    def slow(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 9.0)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(LLMUnavailableError, match="timed out"):
        _provider().enrich_events([_evidence()])


def test_dispatch_from_own_settings(monkeypatch):
    from news_intel.llm.provider import get_provider
    from news_intel.pipeline.editorial_enrichment import build_bounded_provider

    class S:
        llm_provider, claude_bin, claude_config_dir, claude_model, claude_timeout_seconds = (
            "claude_cli",
            "/b/claude",
            "/c",
            None,
            300.0,
        )
        hermes_bin = hermes_timeout_seconds = hermes_model_label = None
        llm_base_url = llm_api_key = llm_model = None

    class C:
        secrets = S()

    assert isinstance(get_provider(C()), ClaudeCliProvider)
    b = build_bounded_provider(C())
    assert isinstance(b, ClaudeCliProvider) and b.timeout == 60.0  # the per-story cap still applies
    S.claude_bin = None
    assert build_bounded_provider(C()) is None  # unconfigured -> source-grounded floor
