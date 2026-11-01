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
        self.calls: list[dict] = []  # one meta record per CLI call, for the run record (latency, tokens, identity)

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
        usage = (result or {}).get("usage") or {}
        self.last_meta = {
            "model": (init or {}).get("model"),
            "cli_version": (init or {}).get("claude_code_version"),
            "api_key_source": (init or {}).get("apiKeySource"),
            "models_used": sorted(((result or {}).get("modelUsage") or {}).keys()),
            "num_turns": (result or {}).get("num_turns"),
            "duration_ms": (result or {}).get("duration_ms"),
            "duration_api_ms": (result or {}).get("duration_api_ms"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "is_error": (result or {}).get("is_error"),
            "exit": proc.returncode,
        }
        self.calls.append(self.last_meta)
        if init is None:
            raise LLMUnavailableError(
                f"claude CLI gave no init event (exit {proc.returncode}): {(proc.stderr or '')[:200]}"
            )
        if init.get("tools") or init.get("mcp_servers"):
            raise LLMUnavailableError("claude CLI isolation failed (tools or MCP servers present); output discarded")
        # 2026-11-01 (news-intel v2): identity must be POSITIVELY established, never assumed from a missing field --
        # the subscription login reports apiKeySource "none"; anything else (or nothing) is refused.
        if init.get("apiKeySource") != "none":
            raise LLMUnavailableError(
                f"claude CLI is not on the subscription login (apiKeySource {init.get('apiKeySource')!r})"
            )
        if init.get("model") != self.requested_model:
            raise LLMUnavailableError(
                f"claude CLI ran model {init.get('model')!r}, not the requested {self.requested_model!r}"
            )
        self.model = f"{init.get('model')} (Claude Code CLI {init.get('claude_code_version')})"
        if result is None or result.get("is_error") or result.get("subtype") != "success":
            why = str((result or {}).get("result") or (result or {}).get("subtype") or f"exit {proc.returncode}")[:200]
            raise LLMUnavailableError(f"claude CLI error: {why}")
        if proc.returncode != 0:
            raise LLMUnavailableError(f"claude CLI exited with status {proc.returncode}; output discarded")
        used = set(self.last_meta["models_used"])
        if not used or used - {self.requested_model}:
            raise LLMUnavailableError(
                f"claude CLI usage reports models {sorted(used)}, not only {self.requested_model}"
            )
        if result.get("num_turns") not in (None, 1) or result.get("permission_denials"):
            raise LLMUnavailableError("claude CLI took more than one turn or attempted a tool; output discarded")
        text = result.get("result") or ""
        if not text.strip():
            raise LLMUnavailableError("claude CLI returned an empty result")
        if len(text) > _MAX_OUTPUT_CHARS:
            raise LLMUnavailableError(f"claude CLI output exceeds bounded size ({len(text)} chars)")
        return text
