# Claude Opus for news-intel (branch `feature/claude-provider`, 2026-11-01)

**Owner request:** news-intel should use the existing, authorized Claude Opus access instead of Hermes's default model.

## Before (production `main`, as checked on the Pi)

**Services:** `pi-news-daily(.zh).service` and friends run as user `star`, with:
- `WorkingDirectory=~/pi-news-intel`;
- `EnvironmentFile=~/pi-news-intel/.env`.

**`.env` settings:** `LLM_PROVIDER=hermes_cli`, `HERMES_BIN=/home/star/.local/bin/hermes`, `HERMES_MODEL_LABEL=hermes-cli`.

**What Hermes actually runs.** `hermes -z` uses Hermes's **global** default model: `mimo-v2.6-pro`, provider `xiaomi`, from `~/.hermes/config.yaml`. That config is shared with other Pi projects. Records showed only the label `hermes-cli`, never the real model.

**Hermes's Claude credential.** The only Anthropic credential in Hermes is a pay-per-token API key, labelled `claude_paid` and never used. Switching Hermes to it would:
- change a global setting other projects depend on;
- bill an API.

Neither is allowed, so Hermes is left untouched.

## Change (minimal, this project only)

**New provider.** `llm/claude_cli.py` adds `ClaudeCliProvider`, selected only by news-intel's own `LLM_PROVIDER=claude_cli` together with `CLAUDE_BIN`, `CLAUDE_CONFIG_DIR` and `CLAUDE_MODEL`.
- It calls the official Claude Code CLI on its claude.ai subscription login.
- It keeps the same contract as `HermesCliProvider`: same JSON retry, and `LLMUnavailableError` leads to the source-grounded floor.
- It fails closed:
  - the CLI's init event must report `apiKeySource: none` and no tools or MCP servers;
  - the child environment never carries API keys.
- `model` is the model the CLI reported, so `job_runs.model_used` shows what actually ran.

**Unchanged:**
- the per-story timeout cap (60 s) and the run budget (150 s);
- Telegram recipients and timers.

**To enable after merge,** change three lines in `~/pi-news-intel/.env`:
```
LLM_PROVIDER=claude_cli
CLAUDE_BIN=/home/star/.local/bin/claude
CLAUDE_CONFIG_DIR=/home/star/.config/history-in-practice/claude   # the existing subscription login (or a dedicated one)
CLAUDE_MODEL=claude-opus-5-5
```

**Rollback:** set `LLM_PROVIDER=hermes_cli` again.

## Dry run (2026-11-01 ~00:30 CT)

**Setup:**
- branch code, a copy of the production DB (sqlite backup, read-only source);
- the real `run_daily(dry_run=True)`;
- no Telegram, no production state.

The job lock copied with the DB was cleared in the copy only.

| Edition | Called | Model reported per story | Result | Fallback reason |
|---|---|---|---|---|
| zh_cn | yes | `claude-opus-5-5 (Claude Code CLI 2.1.308)` | deterministic fallback | validation failed after retry: entity `国铁集团 (China State Railway Group)` not traceable (the model added an English gloss to the Chinese name) |
| en_global | yes | `claude-opus-5-5 (Claude Code CLI 2.1.308)` | deterministic fallback | validation failed after retry: number `5000.0` not traceable to the evidence |

**Conclusion:** the switch to Claude works. Like the earlier MiMo runs, the stories still fall back because of the validator (entity names and number tracing), not because of the model call. That work continues on `feature/news3min-quality`.

**Found in passing (not fixed here):** `pi-news digest daily --dry-run` always reports `skipped_lock`.
- `db.rollback_only` opens a SAVEPOINT.
- `acquire_job_lock`'s `BEGIN IMMEDIATE` then fails inside it, and the failure is caught as "lock not acquired".
- The production (`--no-dry-run`) path is unaffected.

**Tests:** `tests/unit/test_claude_cli_provider.py`. Full suite 865 passed; `ruff check .` clean.
