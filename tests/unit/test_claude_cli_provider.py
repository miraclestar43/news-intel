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
    result = {"type": "result", "subtype": "success", "is_error": is_error, "result": result_text, "duration_ms": 900,
              "num_turns": 1, "modelUsage": {model: {}}, "usage": {"input_tokens": 10, "output_tokens": 5}}
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


def _run_with(monkeypatch, stdout, returncode=0):
    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _P(stdout, returncode))
    p = _provider()
    return p, lambda: p._run_once("hi")


def _edit(stream, **changes):
    init, result = (json.loads(x) for x in stream.strip().splitlines())
    for k, v in changes.items():
        tgt, key = k.split("__")
        (init if tgt == "init" else result)[key] = v
    return json.dumps(init) + "\n" + json.dumps(result) + "\n"


@pytest.mark.parametrize(
    "changes, returncode, why",
    [
        ({"init__apiKeySource": None}, 0, "subscription"),  # identity missing is not identity confirmed
        ({"init__model": "claude-sonnet-5-5"}, 0, "not the requested"),
        ({"result__modelUsage": {"claude-opus-5-5": {}, "claude-haiku-5-5": {}}}, 0, "usage reports"),
        ({"result__modelUsage": {}}, 0, "usage reports"),
        ({"result__num_turns": 3}, 0, "more than one turn"),
        ({"result__permission_denials": [{"tool": "Bash"}]}, 0, "more than one turn"),
        ({"result__subtype": "error_max_turns"}, 0, "claude CLI error"),
        ({"result__result": "   "}, 0, "empty result"),
        ({}, 1, "exited with status 1"),
    ],
)
def test_unconfirmed_identity_or_abnormal_exit_is_never_accepted(monkeypatch, changes, returncode, why):
    p, call = _run_with(monkeypatch, _edit(_stream("OK"), **changes), returncode)
    with pytest.raises(LLMUnavailableError, match=why):
        call()
    assert p.calls and p.calls[-1]["exit"] == returncode


def test_each_call_is_recorded_with_identity_latency_and_tokens(monkeypatch):
    p, call = _run_with(monkeypatch, _stream("OK"))
    assert call() == "OK"
    m = p.calls[-1]
    assert m["model"] == "claude-opus-5-5" and m["models_used"] == ["claude-opus-5-5"] and m["api_key_source"] == "none"
    assert m["duration_ms"] == 900 and m["output_tokens"] == 5 and m["num_turns"] == 1
