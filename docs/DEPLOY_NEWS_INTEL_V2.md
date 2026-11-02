# Deploying news-intel v2 and the newsletter digest (one-time package)

**Status:** prepared, not applied. No step below has been run on production.

**What it deploys:**
- news-intel PR `feature/news-intel-v2` → `main`. It includes `feature/claude-provider` (b4bf28f) and
  `feature/news3min-quality` (bd364a2).
- history-in-practice PR `feature/news-digest-consumer` → `main`.

**Why the order matters.** news-intel runs from its checkout: merging into `main` in `~/pi-news-intel` is live
within 5 minutes (docs/GIT_WORKFLOW.md). history-in-practice only shows news once `NEWS_EXPORT_DIR` is set.

## 1. Order

| Step | What | Live effect |
|---|---|---|
| A | Back up (section 3). | none |
| B | Merge the news-intel PR in `~/pi-news-intel` with `git merge --no-ff` (fast-forward is possible, but a merge commit allows a one-step revert). | Next collect tick fetches 9 new feeds. Dailies use the new prompt and validator. Still on Hermes until step C. |
| C | Edit `~/pi-news-intel/.env` (section 2). | Dailies and the brief write with claude-opus-5-5 via the Claude Code CLI. |
| D | Link and enable `pi-news-brief.timer` only. | A frozen brief every day at 20:05 Beijing (retry 20:50). Nothing is sent. |
| E | Watch 1-2 editions: `python -m news_intel.jobs.brief show`. | none |
| F | Merge the history-in-practice PR and set `NEWS_EXPORT_DIR` (section 2). | The 22:08 email ends with the digest and its archive link. The archive page is committed with the story. |
| G | Optional, owner's choice: enable `pi-news-brief-telegram.timer` (21:00 Beijing) and decide on `pi-news-daily-zh.timer` (section 4). | The full brief reaches Telegram. |

Each step can stop there. Steps D-G each need the previous one.

## 2. Configuration changes

**`~/pi-news-intel/.env`** (mode 600; back it up first, never print it):
```
LLM_PROVIDER=claude_cli                      # was hermes_cli
CLAUDE_BIN=/home/star/.local/bin/claude
CLAUDE_CONFIG_DIR=/home/star/.config/history-in-practice/claude
CLAUDE_MODEL=claude-opus-5-5
BRIEF_CHAT_ID=<the single allowlisted chat id>   # only for step G
```
- **`CLAUDE_CONFIG_DIR`** is history-in-practice's subscription login, shared. A dedicated login for news-intel is cleaner. It needs one interactive `claude` login by the owner with `CLAUDE_CONFIG_DIR=~/.config/news-intel/claude`. The login expires around 2026-11-29 either way.
- **Hermes:** its global config is not touched.

**history-in-practice runtime env: `~/.config/history-in-practice/runtime.env`** (mode 600; back it up first).
- Checked 2026-11-02: `config.DEFAULT_RUNTIME_ENV` is `$HIP_RUNTIME_ENV`, falling back to
  `~/.config/history-in-practice/runtime.env`.
- The installed `hip-send.service` sets neither `Environment` nor `EnvironmentFile`, so the fallback is the live path.
- There is no `~/history-in-practice/data/runtime.env`.
```
NEWS_EXPORT_DIR=/home/star/pi-news-intel/data/exports/brief
```
Unset it to switch the digest off. The email is then byte-identical to the current one.

## 3. Backup and rollback

**Before step B:**
```
cd ~/pi-news-intel && git rev-parse HEAD                  # expect f97b73c
cp -p .env .env.bak-$(date +%Y%m%d)
.venv/bin/python -c "import sqlite3;s=sqlite3.connect('file:data/news-intel.db?mode=ro',uri=True);d=sqlite3.connect('$HOME/backups/news-intel-pre-v2.db');s.backup(d)"
cp -p ~/.config/history-in-practice/runtime.env ~/.config/history-in-practice/runtime.env.bak-$(date +%Y%m%d)
```
The database is about 106 MB, a short copy.

**Rollback, news-intel:**
- `git revert -m 1 <merge sha>` in `~/pi-news-intel`. Or, immediately: `git switch --detach f97b73c`, then fix `main` later.
- Restore `.env` from the backup.
- `systemctl --user disable --now pi-news-brief.timer pi-news-brief-telegram.timer`.
- No migration to undo: the branch adds none.
- The 9 new feeds' rows stay in the database. They are harmless and are pruned by the normal retention.

**Rollback, history-in-practice:**
- Remove `NEWS_EXPORT_DIR`. The digest stops at the next edition.
- `git revert -m 1 <merge sha>` if the code itself must go.
- Bindings already made stay in `data/news_bindings/` as a record.

## 4. Real effects, listed

**Collection.**
- 9 new RSS feeds:
  - zh_cn: 中国新闻网 国内/国际, BBC 中文, RFI 中文, 德国之声中文;
  - en_global: Guardian World, Al Jazeera, France 24, SCMP China.
- On a Sunday the first fetch added 342 items. Expect a few hundred per day.
- robots.txt was checked for each feed.

**Existing dailies (en_global 05:00 UTC, zh_cn 07:00 Los Angeles).** These change too:
- **Candidate pool:** the new feeds enter it. General news can now appear, subject to the dailies' unchanged tech/markets ranking (Unclassified still ×0.0 there).
- **Per-story text:**
  - the new prompt and stricter validator: verbatim quotes per field, numbers bound to their own quotes, no glosses, why only with evidence;
  - with Claude: replay latency 3-23 s per call, so the 150 s run budget covers 3-4 stories;
  - with Hermes/MiMo (step C not done): expect more fallbacks than with Claude.
- **Run record:** `fallback_used` is now true whenever ANY top story is on the floor. Before, one validated story made the whole digest "not fallback". `model_used` says `[mixed: k/n …]`.
- **CLI `--dry-run`:** it now really runs, on a private copy of the database. It holds no production lock and never re-fetches sources against production state.

**New job.** `pi-news-brief`:
- reads the database and writes only its own `job_runs` row and lock;
- writes `data/exports/brief/<date>/`, about 30 KB per day;
- at 13-25 Claude CLI calls per edition, it took 150-400 s in replays.
- **Exit codes** (the unit shows as failed on any non-zero code, which is intended):
  - 0: built / reused / completed;
  - 75: retry_later, a technical failure before the 20:50 last attempt. Nothing is frozen; the evidence goes to
    `exports/brief/<date>/attempts/`.
  - 3: built_degraded, the last attempt published fewer verified items;
  - 1: failed, nothing published; the newsletter goes out without news.
- **Before relying on a day's brief,** check `journalctl --user -u pi-news-brief` and `<date>/current.json`.

**Telegram.** Nothing new is sent until step G.
- With step G, the chat gets the brief at 21:00 Beijing.
- `pi-news-daily-zh.timer` still sends the old zh daily at 07:00 Los Angeles, which is 23:00 Beijing now that US DST has ended. Recommendation: disable it once the brief is on Telegram. The two overlap and the old one is tech-only.

**Newsletter (step F).** The 22:08 email:
- ends with the digest, which comes from the frozen brief bound once, durably, at the site step. The email step and
  `hip send --real` only read that binding; no binding means no news, so a retry can never add news later;
- binds only an archive page whose meta snapshot_id and text_sha256 equal the bound brief's;
- adds `history/news/<date>/index.html` to the site commit, the page the digest links to;
- goes out with the story only, with the reason in the publish report, when news is missing, stale, damaged or unqualified.

**Known, not changed here.**
- The en_global daily timer is fixed at 05:00 UTC, which is 21:00 Los Angeles since 2026-11-01.
- The repository's `deploy/systemd/pi-news-daily.service` differs from the installed one (pre-existing drift).

## 5. Commands for steps D and G (when approved)

```
ln -s ~/pi-news-intel/deploy/systemd/pi-news-brief.service ~/.config/systemd/user/
ln -s ~/pi-news-intel/deploy/systemd/pi-news-brief.timer ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pi-news-brief.timer
systemctl --user list-timers | grep -E 'brief|daily|hip-send'
# step G (owner's choice):
ln -s ~/pi-news-intel/deploy/systemd/pi-news-brief-telegram.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now pi-news-brief-telegram.timer
```
**Check after each step:**
- `hermes-gateway.service` and the polymarket services are still `active`;
- `~/.hermes/config.yaml`'s mtime is unchanged;
- the next collect run's journal is clean.
