# Git consolidation, 2026-10-17

The repository was consolidated from nine worktrees and twelve branches down to
one checkout, `main`, and two retained work branches. Nothing was merged into
production code, and nothing was pushed to GitHub. A security blocker stops the
push (see section 5).

## 1. Original inventory (before any change)

Repository root: `/home/star/pi-news-intel/.git`. No remote was configured. The
default branch was `master` @ `70a29b8`. There were no stashes and no detached
HEADs. One tag existed: `baseline/prod-2026-09-21` (annotated, pointing at
commit `673c954`). There were 117 commits across all refs.

### Worktrees

| Directory | Git branch | HEAD SHA | Working tree status | Unique work vs master | Runtime dependency | Action taken |
|---|---|---|---|---|---|---|
| `pi-news-intel` | master | 70a29b8 | untracked `.claude/`, `evidence/`, `.env.bak.*`, `data` symlink | none | **production**: every systemd unit, `.venv` editable install | kept; branch renamed to `main` |
| `pi-news-intel-hotfix` | hotfix/telegram-html-2026-09-22 | 70a29b8 | clean | none (equal to master) | none | worktree removed, branch deleted (`-d`) |
| `pi-news-intel-prodpatch` | fix/deterministic-2026-09-21 | 5148035 | clean | 7 commits; the narrow production patch, ready but **not deployed** | none | worktree removed, **branch kept** |
| `pi-news-intel-round8` | quarantine/round8-collision-20260817 | f10b85b | 1 modified file: `src/news_intel/cli.py` (edition wiring) | 4 commits; superseded, non-deployable | none | diff backed up; tag `archive/round8-quarantine-collision`; worktree removed; branch deleted |
| `pi-news-intel-round8-authoritative` | repair/round8-authoritative | f9fba63 | clean (+ `.venv`) | none (merged) | none | worktree removed, branch deleted (`-d`) |
| `pi-news-intel-round8-integration` | integration/multiedition-round8 | 94908d3 | clean (+ `.venv`) | none (merged) | none | worktree removed, branch deleted (`-d`) |
| `pi-news-intel-round8-recovery` | repair/en-global-round8-recovery | bcaaf01 | clean | 1 commit; superseded | none | tag `archive/round8-en-global-recovery`; worktree removed; branch deleted |
| `pi-news-intel-round8-zh` | feature/zh-cn-round8-recovery | 37baf0d | clean (+ `.venv`) | none (merged) | none | worktree removed, branch deleted (`-d`) |
| `pi-news-intel-round9-incident` | quality/intel-architecture | d7e1916 | clean (+ `.venv`) | 19 commits: Phase C | none | worktree removed, **branch kept** |

### Branches with no worktree

| Branch | HEAD | Unique work | Action taken |
|---|---|---|---|
| repair/aug19-aug20-incident | 673c954 | none (merged) | deleted (`-d`) |
| round8-daily-weekly-repair | 1950824 | 3 commits, the same patches as the quarantine branch | tag `archive/round8-daily-weekly-repair`; deleted |

### Non-git `pi-news-intel-*` directories (all left untouched)

| Directory | What it is | Why it stays |
|---|---|---|
| `pi-news-intel-canary-data` | **the live production database.** `pi-news-intel/data` is a symlink to it. | runtime dependency; the name is historical |
| `pi-news-intel-eval` | read-only replay DB snapshots with sha256 checksums | evaluation evidence for Phase C |
| `pi-news-intel-backups`, `pi-news-intel-canary-incident-backup-2026-08-06` | earlier backups | evidence |
| `pi-news-intel-content-quality-round{3..9}-evidence`, `pi-news-intel-hermes-content-round2-evidence` (+ `.sha256`) | QA evidence | evidence |
| `pi-news-intel-phase6-data`, `pi-news-intel-shortcheck-data` | historical run data | data |

These could later be moved together under one archive directory, as long as the
`data` symlink target is handled first. That was not done here.

## 2. How the superseded work was judged

- **Quarantine (`f10b85b`)** was not merged or cherry-picked. Its zh_cn work was
  rebuilt cleanly as `feature/zh-cn-round8-recovery` and
  `integration/multiedition-round8`, both already in master. Every file in the
  quarantine tree already exists in master (`git diff --name-status`: no `A`
  entries). The uncommitted `cli.py` change (an `--edition` option for
  `production daily`) is already in master: see `edition: str = _EDITION_OPTION`
  and `run_daily(..., edition_id=edition)`.
- **`bcaaf01` (en-global round 8 recovery)** was superseded by
  `repair/round8-authoritative`, which is merged: `c3741fe` (English repairs),
  `941aede` (weekly entrypoint, migration 011), and `54b7f93` ("Port … vocabulary
  from bcaaf01 review"). Migration 011, the weekly unit, the policy and the
  round-3 tests are byte-identical in master. The remaining files have since
  evolved in master.

## 3. Status of the work that was kept

**`quality/intel-architecture` @ `d7e1916` (2026-09-25).** Phase C steps 1a–1e,
2–6b and 7–10 are all committed:

- `e11380e`: persistent developments, structured delta, themes, and claim
  grounding.
- `d7e1916`: replay-driven fixes, acceptance/gold tests, a brief report, and the
  shadow-run plan.

This work is **complete in code, isolated, and not merged**:

- It is validated only against provisional (frozen) labels.
- The shadow run (`docs/QUALITY_ARCHITECTURE.md` §9) is proposed but has not run.
- All behaviour sits behind `select_for_window` flags that default to off.
- Standing instruction: do not deploy the ranker.
- It adds `migrations/014_developments.sql`, which would auto-apply to the live
  database on the next CLI run after a merge.

Merging it is a deployment decision, so it was not merged. It merges into `main`
without conflicts (`git merge-tree`).

**`fix/deterministic-2026-09-21` @ `5148035`.** This is the narrow production
patch:

- harness
- point-in-time windows
- word-boundary matcher
- language detection
- thin-source rejection
- windowed GDELT collector

It is ready but has not been deployed, pending approval. It changes
`config/policy.yaml` and `config/sources.yaml`. All six of its patches also exist
in the quality branch (`git cherry`: all `-`). It merges into `main` without
conflicts.

## 4. Backup

Location: `/home/star/backups/news-intel-git-consolidation-20261017/` (directory
mode `700`, files mode `600`; outside every project directory, never published).

| File | Contents |
|---|---|
| `pi-news-intel-all-refs.bundle` | `git bundle --all`: all 12 refs, 117 commits |
| `refs-at-backup.txt`, `worktrees-at-backup.txt`, `git-config.txt` | ref, worktree and config state before the change |
| `worktree-changes/*.status`, `*.diff` | per-worktree uncommitted state; only `pi-news-intel-round8.diff` is non-empty |
| `worktree-changes/pi-news-intel-round8.cli.py.worktree-copy` | full copy of the modified file |
| `untracked/prod-tree-untracked.tgz` | `.claude/`, `evidence/`, `.env`, `.env.bak.*`, `data-gate-a-completed-2026-08-02/` |
| `db/news-intel.db` | consistent snapshot of the live DB via the SQLite online backup API |
| `SHA256SUMS` | checksums |

Verification evidence:

- `git bundle verify`: OK.
- Mirror-cloned the bundle and compared `for-each-ref` with the original: all 12
  refs identical. `git fsck`: clean. 117 commits.
- `git apply --check` of the round8 diff onto a fresh `f10b85b` checkout: clean.
  Before the worktree file was reverted, the live diff was byte-identical
  (`cmp`) to the backed-up diff.
- DB snapshot: `PRAGMA integrity_check` = ok; 19 tables; 29,883 events; 109
  deliveries.

Restoring a removed branch: `git fetch <bundle> 'refs/heads/*:refs/heads/*'`,
or directly from the in-repo `archive/*` tags.

## 5. GitHub remote status: BLOCKED (security)

`miraclestar43/news-intel` is **public** and empty (no branches, size 0). No
`origin` was added and nothing was pushed.

A full-history scan (all 117 commits, plus the live values in every `.env` on
this machine) found:

- **The live production Telegram bot token is committed.** It is the
  `_FAKE_TOKEN` literal in `tests/unit/test_log_redaction_incident.py`,
  introduced in `5209b12` (2026-08-29). It is present in 32 blobs, so it is
  reachable from every later commit on `main` and on both kept branches. The
  incident that test describes had also written the token to the systemd journal.
- Not found in history: the allowed chat ID, and tokens from the other bots and
  projects on this Pi (hermes, polymarket). No `.env`, database, log or key file
  was ever committed; only `.env.example`.
- Commit metadata exposes the author address `lisida43@vip.qq.com`, which would
  become public.

Done here: the working tree now uses a shape-accurate fake token (commit on
`main`, tests only). **This does not clean history.**

Required before any push (owner decisions):

1. **Revoke and rotate the bot token** with BotFather, then update
   `TELEGRAM_BOT_TOKEN` in `.env`. Required whatever else is chosen, because the
   token was already exposed in the journal.
2. Choose how to publish:
   - (a) rewrite local history with `git filter-repo --replace-text` to remove
     the token (and optionally `--mailmap` the author email);
   - (b) publish a fresh single-commit `main` and keep the full history local
     only; or
   - (c) after rotation, publish the history as is, since the token would be
     dead.

   (a) or (b) is recommended. The GitHub repo is empty, so none of these needs a
   force-push.
3. Then: `git remote add origin https://github.com/miraclestar43/news-intel.git`
   and `git push -u origin main`, pushing only `main`, with no `--tags` or
   `--all`.

## 6. Final state

- Local branches: `main` @ (this commit), `fix/deterministic-2026-09-21`
  @ `5148035`, `quality/intel-architecture` @ `d7e1916`.
- Tags: `baseline/prod-2026-09-21`, `archive/round8-quarantine-collision`,
  `archive/round8-en-global-recovery`, `archive/round8-daily-weekly-repair`.
- Worktrees: only `/home/star/pi-news-intel [main]`; `git worktree prune`
  found nothing stale.
- Branch rename `master` → `main`: nothing in the code, units or deploy files
  referenced `master`.

## 7. Production verification

- Runtime code is unchanged. HEAD stayed `70a29b8` throughout cleanup; the only
  later commit touches `tests/` and `docs/`.
- Timers are unchanged: `pi-news-collect` (every 5 min), `pi-news-daily` and
  `pi-news-daily-zh` are enabled; weekly, monthly and alert-shadow remain
  disabled. `hermes-gateway` is active.
- `pi-news-collect.service`: `Result=success` after the cleanup.
- Live DB: `integrity_check` ok; 29,883 events and 109 deliveries, matching the
  backup.
- `.env` is untouched (mode 600).
- `.venv` resolves `news_intel` from `/home/star/pi-news-intel/src`. Imports and
  `pi-news --help` work. `git ls-files -d`: 0 missing files.
- `pytest`: **859 passed**. `ruff check .`: all checks passed. No Telegram send
  was triggered, and no flag or config was changed.

## 8. Unresolved

1. Token rotation and the publication choice (section 5). This blocks GitHub.
2. Whether to deploy `fix/deterministic-2026-09-21`: an owner decision, which
   means merging into `main` in the production checkout.
3. The `quality/intel-architecture` shadow run and label validation, before any
   merge.
4. `.claude/project/CURRENT.md` is stale (last updated 2026-08-17).
5. `pi-news-daily.timer` uses a fixed-UTC `OnCalendar` (05:00 UTC), and DST
   falls back on 2026-11-01.
6. `.env.bak.20260825024316` also holds the live token, in plaintext, untracked,
   mode 600.
