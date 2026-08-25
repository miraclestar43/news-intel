"""2026-08-24: HermesCliProvider. Every test monkeypatches subprocess.run --
never spawns a real process or touches the real hermes binary/config, per
this repo's established test-isolation practice. Proves: argv-list/
shell=False invocation shape, tools-disabled flag, timeout -> safe
LLMUnavailableError, oversized prompt/output rejected before/after the
subprocess call, non-zero exit -> LLMUnavailableError, one retry on
invalid JSON, and that cited_fact_ids/category flow through end to end."""

from __future__ import annotations

import subprocess

import pytest

from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.hermes_cli import HermesCliProvider
from news_intel.models import EventInput, EvidenceFact

GOOD_JSON = (
    '{"event_id": "E-1", "category": "AI & Semiconductors", "subcategories": [], "entities": ["Starcloud"], '
    '"english_headline": "Starcloud raises $250M.", "what_happened": "Starcloud raised $250 million.", '
    '"why_it_matters": "It extends Starcloud\'s runway for orbital AI inference.", '
    '"chinese_headline": "Starcloud融资2.5亿美元", "chinese_what_happened": "Starcloud获得2.5亿美元融资。", '
    '"chinese_takeaway": "Starcloud获得2.5亿美元融资，用于轨道AI数据中心扩张。", '
    '"significance_proposal": 70, "market_impact_proposal": 50, "material_facts": [], "uncertainties": [], '
    '"supporting_source_ids": ["s0"], "cited_fact_ids": ["f1"]}'
)


def _evidence():
    return EventInput(
        event_id="E-1", candidate_title="Starcloud raises $250 million",
        sources=[{"id": "s0", "publisher": "TechCrunch", "excerpt": "Starcloud raised $250 million.",
                  "published_at": "2026-08-21T15:00:00+00:00"}],
        edition_id="en_global", category_candidates=["AI & Semiconductors"],
        facts=[EvidenceFact(id="f1", kind="actor", text="Starcloud")],
    )


class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_invokes_argv_list_shell_false_with_tools_disabled(monkeypatch):
    captured = {}

    def fake_run(argv, shell, capture_output, text, timeout):
        captured["argv"] = argv
        captured["shell"] = shell
        captured["timeout"] = timeout
        return _FakeCompletedProcess(stdout=GOOD_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes", timeout=9.0)
    results = provider.enrich_events([_evidence()])

    assert captured["shell"] is False
    assert captured["timeout"] == 9.0
    argv = captured["argv"]
    assert argv[0] == "/usr/local/bin/hermes"
    assert argv[1] == "-z"
    assert "-t" in argv
    assert argv[argv.index("-t") + 1] == ""  # empty toolset string -- zero tools enabled
    assert "--ignore-rules" in argv
    assert results[0].event_id == "E-1"
    assert results[0].cited_fact_ids == ["f1"]


def test_no_source_text_interpolated_into_a_shell_string(monkeypatch):
    """The prompt (which embeds untrusted source excerpts) must appear as
    ONE argv element, never split/concatenated into shell syntax."""
    captured = {}

    def fake_run(argv, shell, capture_output, text, timeout):
        captured["argv"] = argv
        return _FakeCompletedProcess(stdout=GOOD_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    evidence = _evidence()
    evidence = evidence.model_copy(update={"sources": [{**evidence.sources[0],
                                                          "excerpt": "malicious `rm -rf /` text; $(whoami)"}]})
    HermesCliProvider(bin_path="/usr/local/bin/hermes").enrich_events([evidence])

    prompt_arg = captured["argv"][2]
    assert "rm -rf" in prompt_arg  # present as inert text, not executed
    assert captured["argv"].count(prompt_arg) == 1  # exactly one argv element, not shell-split


def test_timeout_raises_llm_unavailable(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes", timeout=5.0)
    with pytest.raises(LLMUnavailableError, match="timed out"):
        provider.enrich_events([_evidence()])


def test_nonzero_exit_raises_llm_unavailable(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout="", returncode=1, stderr="some CLI error")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError, match="exited with status 1"):
        provider.enrich_events([_evidence()])


def test_executable_not_found_raises_llm_unavailable(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/nonexistent/hermes")
    with pytest.raises(LLMUnavailableError, match="could not be invoked"):
        provider.enrich_events([_evidence()])


def test_oversized_prompt_rejected_before_subprocess_call(monkeypatch):
    called = {"n": 0}

    def fake_run(*a, **k):
        called["n"] += 1
        return _FakeCompletedProcess(stdout=GOOD_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    evidence = _evidence()
    huge_excerpt = "x" * 30_000
    evidence = evidence.model_copy(update={"sources": [{**evidence.sources[0], "excerpt": huge_excerpt}]})
    with pytest.raises(LLMUnavailableError, match="bounded size"):
        provider.enrich_events([evidence])
    assert called["n"] == 0


def test_oversized_output_rejected(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout="x" * 25_000)

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError, match="bounded size"):
        provider.enrich_events([_evidence()])


def test_retries_once_on_invalid_json_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_run(argv, shell, capture_output, text, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            return _FakeCompletedProcess(stdout="not json at all")
        return _FakeCompletedProcess(stdout=GOOD_JSON)

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    results = provider.enrich_events([_evidence()])
    assert calls["n"] == 2
    assert results[0].event_id == "E-1"


def test_invalid_json_both_attempts_raises(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout="still not json")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError, match="did not return valid JSON"):
        provider.enrich_events([_evidence()])


def test_strips_markdown_fences(monkeypatch):
    def fake_run(argv, shell, capture_output, text, timeout):
        return _FakeCompletedProcess(stdout=f"```json\n{GOOD_JSON}\n```")

    monkeypatch.setattr(subprocess, "run", fake_run)
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    results = provider.enrich_events([_evidence()])
    assert results[0].event_id == "E-1"


def test_model_label_defaults_to_hermes_cli():
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    assert provider.model == "hermes_cli"


def test_model_label_overridable():
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes", model_label="mimo-v2.5")
    assert provider.model == "mimo-v2.5"


def test_compose_daily_not_implemented_raises_llm_unavailable():
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError):
        provider.compose_daily(None)


def test_compose_weekly_not_implemented_raises_llm_unavailable():
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError):
        provider.compose_weekly(None)


def test_explain_event_not_implemented_raises_llm_unavailable():
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    with pytest.raises(LLMUnavailableError):
        provider.explain_event(None)


def test_has_no_telegram_or_delivery_capability():
    """Structural proof, matching the existing editorial-layer suite's
    approach: this provider object carries no chat_id, no TelegramSender,
    and no send-like method -- it cannot itself contact Telegram."""
    provider = HermesCliProvider(bin_path="/usr/local/bin/hermes")
    forbidden = ("send", "telegram", "chat_id", "deliver")
    for attr in dir(provider):
        assert not any(f in attr.lower() for f in forbidden), attr
