"""LLMProvider that calls the official Claude Code CLI on the Pi (owner decision 2026-11-01: news-intel uses the
existing authorized Claude Opus access -- the claude.ai subscription login -- instead of Hermes's default model).

Why not Hermes: `hermes -z` follows Hermes's GLOBAL default model (mimo-v2.6-pro), shared with other Pi projects; the
only Anthropic credential Hermes holds is a pay-per-token API key. Changing either would affect other projects or bill
an API. This provider leaves Hermes untouched and is selected only by pi-news-intel's own LLM_PROVIDER=claude_cli.

Same contract as HermesCliProvider (it reuses its JSON repair/retry and enrich_events): a bounded, no-tools, one-shot
subprocess; any failure raises LLMUnavailableError so callers take the documented source-grounded floor.

Fail-closed, like history-in-practice/claude_cli.py:
* subscription only -- the child environment is an allowlist that never carries ANTHROPIC_API_KEY/AUTH_TOKEN,
  CLAUDE_CODE_OAUTH_TOKEN or provider switches, and the CLI's own init event must report apiKeySource "none";
* isolated -- --safe-mode, --tools "", --strict-mcp-config, no session persistence, slash commands off, an empty
  working directory, a dedicated CLAUDE_CONFIG_DIR; any tool or MCP server in the init event -> output discarded;
* `self.model` is the model the CLI reported for the last call (never a configured label), so job records and the
  provider_outcome show what actually ran.
Never prints or reads credentials.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile

from news_intel.llm.errors import LLMUnavailableError
from news_intel.llm.hermes_cli import _MAX_OUTPUT_CHARS, _MAX_PROMPT_CHARS, HermesCliProvider

_ENV_ALLOW = ("HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
_SYSTEM = (
    "You are a careful text-processing assistant inside a batch job. You have no tools and no files: everything "
    "you need is in the user message. Follow its instructions exactly and reply with the requested output only."
)
_MAX_STREAM_BYTES = 2_000_000


class ClaudeCliProvider(HermesCliProvider):
    def __init__(self, bin_path: str, config_dir: str, model: str = "claude-opus-5-5", timeout: float = 60.0):
        super().__init__(bin_path=bin_path, timeout=timeout, model_label=None)
        self.config_dir = config_dir
        self.requested_model = model
        self.model = f"claude_cli:{model} (not yet called)"
        self.last_meta: dict = {}

    def _env(self) -> dict:
        env = {k: os.environ[k] for k in _ENV_ALLOW if k in os.environ}
        env.update(
            PATH="/usr/local/bin:/usr/bin:/bin",
            CLAUDE_CONFIG_DIR=self.config_dir,
            DISABLE_AUTOUPDATER="1",
            DISABLE_TELEMETRY="1",
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        )
        return env

    def _run_once(self, prompt: str) -> str:
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise LLMUnavailableError(f"prompt exceeds bounded size ({len(prompt)} > {_MAX_PROMPT_CHARS} chars)")
        argv = [
            self.bin_path,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--safe-mode",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--strict-mcp-config",
            "--no-session-persistence",
            "--permission-prompts",
            "none",
            "--disable-slash-commands",
            "--setting-sources",
            "user",
            "--model",
            self.requested_model,
            "--system-prompt",
            _SYSTEM,
        ]
        with tempfile.TemporaryDirectory(prefix="news-claude-") as wd:
            try:
                proc = subprocess.run(  # noqa: S603 -- argv list, shell=False, absolute path, prompt on stdin
                    argv,
                    shell=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    input=prompt,
                    cwd=wd,
                    env=self._env(),
                )
            except subprocess.TimeoutExpired as exc:
                raise LLMUnavailableError(f"claude CLI timed out after {self.timeout}s") from exc
            except OSError as exc:
                raise LLMUnavailableError(f"claude CLI could not be invoked: {exc}") from exc
        out = proc.stdout or ""
        if len(out) > _MAX_STREAM_BYTES:
            raise LLMUnavailableError(f"claude CLI stream exceeds bounded size ({len(out)} chars)")
        init, result = None, None
        for line in out.splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "system" and ev.get("subtype") == "init":
                init = ev
            elif ev.get("type") == "result":
                result = ev
        self.last_meta = {
            "model": (init or {}).get("model"),
            "cli_version": (init or {}).get("claude_code_version"),
            "api_key_source": (init or {}).get("apiKeySource"),
            "duration_ms": (result or {}).get("duration_ms"),
            "is_error": (result or {}).get("is_error"),
        }
        if init is None:
            raise LLMUnavailableError(
                f"claude CLI gave no init event (exit {proc.returncode}): {(proc.stderr or '')[:200]}"
            )
        if init.get("tools") or init.get("mcp_servers"):
            raise LLMUnavailableError("claude CLI isolation failed (tools or MCP servers present); output discarded")
        if init.get("apiKeySource") not in (None, "none"):
            raise LLMUnavailableError(
                f"claude CLI is not on the subscription login (apiKeySource {init.get('apiKeySource')!r})"
            )
        self.model = f"{init.get('model') or self.requested_model} (Claude Code CLI {init.get('claude_code_version')})"
        if result is None or result.get("is_error"):
            why = str((result or {}).get("result") or (result or {}).get("subtype") or f"exit {proc.returncode}")[:200]
            raise LLMUnavailableError(f"claude CLI error: {why}")
        text = result.get("result") or ""
        if len(text) > _MAX_OUTPUT_CHARS:
            raise LLMUnavailableError(f"claude CLI output exceeds bounded size ({len(text)} chars)")
        return text
