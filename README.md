# News Intel

**English** · [简体中文](README.zh-CN.md)

A low-noise personal news intelligence system that runs on a Raspberry Pi. It
collects news from official and high-quality sources, merges duplicates into
events, decides how well confirmed each event is, scores what matters, and
delivers a short daily, weekly and monthly brief to Telegram, in English and
in Chinese.

The goal is to read fewer, better items: a handful of stories that matter, each
with its source and why it matters, instead of a firehose.

## Features

- **Collection every 5 minutes** from about 25 configurable sources: RSS/Atom
  feeds, the Federal Register API, Hacker News, BLS/BEA release calendars
  (ICS), the GDELT DOC API, and config-driven HTML index pages (gov.cn, NDRC,
  the PRC MFA spokesperson).
- **Dedupe and event clustering**: exact dedupe by ID, canonical URL and
  content hash, then clustering by title similarity and entity overlap.
- **Deterministic confirmation**: each event moves through
  `unverified → reported → confirmed → official`, driven by source tier and
  independent origin families. The LLM never decides this.
- **Rule-based scoring** with topic vocabularies you can tune without changing
  code (AI, semiconductors, policy, macro and markets, China, geopolitics,
  world politics).
- **Two editions**: `en_global` (English, 22:00 Pacific) and `zh_cn` (native
  Chinese, 07:00 Pacific), each with daily, weekly (Sunday) and monthly
  (first Monday) briefs.
- **Optional LLM enrichment** through any OpenAI-compatible endpoint, with a
  deterministic fallback, so the brief still arrives if the LLM is down.
- **Safe Telegram delivery**: a chat-ID allowlist, idempotent sends (a brief is
  never sent twice), HTML-safe splitting under Telegram's 4096-character limit,
  and dry-run by default.
- **Runs on a Pi**: SQLite, systemd user timers, and no other services.

## How it works

```
sources ──► collect ──► normalize + dedupe ──► cluster into events
                                                      │
              Telegram ◄── render ◄── select ◄── score ◄── confirm
                              ▲
                     LLM enrichment (optional, with deterministic fallback)
```

## Requirements

- Python 3.11 or newer (developed on Raspberry Pi OS, Debian 12, arm64; any
  Linux should work)
- A Telegram bot token from [@BotFather](https://t.me/BotFather) and your chat
  ID, needed only for real sends
- Optional: an OpenAI-compatible LLM endpoint

## Installation

The bundled systemd units expect the project at `~/pi-news-intel`:

```bash
git clone https://github.com/miraclestar43/news-intel.git ~/pi-news-intel
cd ~/pi-news-intel
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The SQLite database is created at `data/news-intel.db` on first run, and schema
migrations in `migrations/` apply automatically.

## Configuration

### Secrets

Copy `.env.example`, fill it in, and keep it private:

```bash
mkdir -p ~/.config/pi-news-intel
cp .env.example ~/.config/pi-news-intel/pi-news-intel.env
chmod 600 ~/.config/pi-news-intel/pi-news-intel.env
```

The CLI loads this file automatically. Alternatively, export the variables
yourself or load them with `EnvironmentFile=` in a systemd unit.

| Variable | Required | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | for sending | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | for sending | Comma-separated allowlist; nothing is sent elsewhere |
| `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` | optional | OpenAI-compatible chat-completions endpoint |
| `LLM_PROVIDER`, `HERMES_BIN`, … | optional | Alternative: use a local Hermes CLI as the LLM |
| `SEC_USER_AGENT` | recommended | SEC requires a descriptive User-Agent with contact info |
| `GUARDIAN_API_KEY` | optional | Only if you enable the Guardian source |

Never commit secrets. `.env` and `data/` are git-ignored.

### What to follow

All non-secret behaviour is plain YAML in `config/`:

| File | Controls |
|---|---|
| `sources.yaml` | Sources, adapters, tiers, poll intervals; turn each on or off with `enabled:` |
| `topics.yaml`, `topics_zh.yaml` | Topic groups, weights and vocabulary (English and Chinese) |
| `editions.yaml` | Editions, labels, time zone and delivery hours |
| `policy.yaml` | Scoring, freshness, selection and delivery policy |
| `alert_triggers.yaml` | Breaking-alert rules (off by default) |
| `prompts/` | LLM prompts for enrichment and composition |

## Usage

Start with read-only checks and dry runs:

```bash
.venv/bin/pi-news doctor                  # config/environment check (never prints secrets)
.venv/bin/pi-news collect --dry-run       # one collection pass, all DB writes rolled back
.venv/bin/pi-news collect --no-dry-run    # real collection into SQLite
.venv/bin/pi-news status                  # source health, last runs, queue counts
.venv/bin/pi-news digest daily --dry-run  # render today's brief without sending
.venv/bin/pi-news digest daily --dry-run --edition zh_cn
```

To send to Telegram, review the dry-run output first, then:

```bash
.venv/bin/pi-news digest daily --no-dry-run --chat-id <your_chat_id>
```

For scheduled long-term delivery there is a stricter entrypoint. It refuses to
send before `--start-date` and skips late catch-up sends:

```bash
PRODUCTION_START_DATE=2026-01-01 .venv/bin/pi-news production daily --edition en_global
```

`pi-news health --days 7` shows what actually reached the reader on each day.

## Scheduling with systemd

User-unit templates are in `deploy/systemd/`:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/pi-news-collect.{service,timer} \
   deploy/systemd/pi-news-daily.{service,timer} \
   deploy/systemd/pi-news-daily-zh.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pi-news-collect.timer pi-news-daily.timer pi-news-daily-zh.timer
loginctl enable-linger "$USER"   # keep user timers running without a login session
```

Read each unit before enabling it:

- `pi-news-collect` has no access to secrets.
- The daily template starts in `--dry-run`. Switch it to
  `pi-news production daily` and add `EnvironmentFile=` once you have reviewed
  the output.
- Weekly and monthly units are available. Some other units (`bot`, `held`,
  `cleanup`, `health`) are placeholders for features that are not finished.

## Project layout

```
src/news_intel/   collectors, pipeline (dedupe, cluster, confirm, score), jobs, delivery, llm
config/           YAML configuration and LLM prompts
migrations/       SQLite schema migrations (applied automatically)
deploy/systemd/   systemd user units and timers
tests/            unit and integration tests
docs/             PRD, operations, source policy, Git workflow
```

## Development

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
```

Work on short-lived `feature/*` or `fix/*` branches and merge into `main` after
tests pass; see [docs/GIT_WORKFLOW.md](docs/GIT_WORKFLOW.md). Tests never
contact Telegram. Use obviously fake credentials in fixtures.

## Documentation

- [docs/PRD.md](docs/PRD.md): product requirements and design
- [docs/OPERATIONS.md](docs/OPERATIONS.md): operating notes
- [docs/SOURCE_POLICY.md](docs/SOURCE_POLICY.md): source selection and tiers
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md): acceptance criteria

## Status

A personal project, in daily use on one Raspberry Pi since August 2026. It's
built for one reader's interests, so expect to adjust `sources.yaml` and the
topic files to your own. Some HTML sources depend on page markup and can break
when a site changes; `pi-news status` reports them as `degraded` rather than
failing.
