# Operations

## Install

```bash
cd ~/pi-news-intel
python3.11 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Secrets live outside the repo at `~/.config/pi-news-intel/pi-news-intel.env`
(mode `0600`), following `.env.example`. Non-secret policy lives in
`config/policy.yaml`, `config/sources.yaml`, `config/topics.yaml`.

## CLI

```bash
.venv/bin/pi-news doctor                 # environment/config readiness, no secret values printed
.venv/bin/pi-news status                 # source health, job history, counts
.venv/bin/pi-news collect --dry-run      # preview a collection pass; rolls back all DB writes
.venv/bin/pi-news collect --no-dry-run   # real collection pass, persists to SQLite
.venv/bin/pi-news digest daily --dry-run # generate (not send) today's daily brief
.venv/bin/pi-news digest daily --no-dry-run --chat-id <id>   # real send (Phase 1, once bot token/chat id are configured)
```

## systemd (user units, matching the Hermes/polymarket convention on this Pi)

Unit files are in `deploy/systemd/`. To install:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/pi-news-collect.service deploy/systemd/pi-news-collect.timer \
   deploy/systemd/pi-news-daily.service deploy/systemd/pi-news-daily.timer \
   ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now pi-news-collect.timer
systemctl --user enable --now pi-news-daily.timer   # still --dry-run inside the unit until reviewed
```

`pi-news-weekly`, `pi-news-held`, `pi-news-cleanup`, `pi-news-health`, and
`pi-news-bot` unit files are present but reference CLI subcommands that don't
exist until Phase 2/3 — do not enable them yet.

## Enabling real Telegram sends

1. Create a new, dedicated bot via @BotFather; get its token and your
   private chat id.
2. Write them into `~/.config/pi-news-intel/pi-news-intel.env` (`chmod 600`):
   `TELEGRAM_BOT_TOKEN=...`, `TELEGRAM_ALLOWED_CHAT_IDS=<your_chat_id>`.
3. Run `pi-news digest daily --dry-run` first and review the rendered text.
4. Only then run with `--no-dry-run --chat-id <id>`.

## Enabling MiMo/LLM enrichment

Set `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL` in the same env file, pointing
at an OpenAI-compatible chat-completions endpoint (Hermes's own deployment
uses a `xiaomi`/`mimo-v2.5`-style endpoint at `https://token-plan-cn.xiaomimimo.com/v1`
per `~/.hermes/config.yaml` — this project does **not** read Hermes's own
`.env`; you configure `pi-news-intel`'s own credentials independently).
Without these three variables set, `pi-news` always uses the deterministic
fallback and daily briefs are still produced, just without LLM summarization
or Chinese takeaways.

## Logs, DB, retention

- SQLite: `data/news-intel.db` (WAL mode). Logs: `data/logs/news-intel.log`
  (rotating, 50MB x5).
- Retention policy (raw excerpt 14d, events 180d, etc.) is defined in
  `config/policy.yaml` under `storage:`; a cleanup job/timer is prepared but
  not yet implemented (Phase 2+).
