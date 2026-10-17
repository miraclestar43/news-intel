# Git workflow

## The one fact that shapes everything

Production runs **directly from this checkout**: every systemd unit uses
`WorkingDirectory=%h/pi-news-intel` and `%h/pi-news-intel/.venv`, which is an
editable install of `src/`. `db.run_migrations` runs on every CLI start.

So **whatever `main` holds in `/home/star/pi-news-intel` is live within five
minutes** (the next `pi-news-collect.timer` tick), including new migrations.
Moving `main` in this checkout *is* a deployment. Treat it that way.

## Rules

1. `main` is the only persistent branch. It is the integration branch and the
   deployed branch.
2. Develop on short-lived `feature/<topic>` or `fix/<topic>` branches.
3. Use a temporary worktree only when isolation is needed:
   `git worktree add ../pi-news-intel-wt-<topic> -b fix/<topic> main`.
   Never edit `src/` in the production checkout directly.
4. One session per worktree. Never let two sessions edit the same worktree, or
   edit production code concurrently in several worktrees, without
   coordination (see the 2026-08-17 Round 8 collision).
5. Finish on the branch: `pytest` and `ruff check .` pass, the change is
   reviewed, and anything that changes selection or ranking stays behind a flag
   that defaults to off.
6. Merging into `main` is a deliberate, approved deployment step, done in the
   production checkout with `git merge --ff-only` or `--no-ff`. Before you
   merge, check for new migrations and config defaults. A merge never enables
   a feature flag; enabling one is its own explicit commit.
7. Push the integration commit to GitHub (`git push origin main`), and only
   after the secret checks below pass.
8. Verify production: `systemctl --user list-timers`, check the next collect
   run's journal, run `pytest`. Do not trigger a real Telegram send to test.
9. Remove the temporary worktree (`git worktree remove <path>`, without
   `--force`) and delete the local branch (`git branch -d`).
10. During normal operation, `git branch` shows only `main`. Unfinished work
    that must outlive a session stays on its branch, or gets an
    `archive/<name>` tag plus a bundle backup; it is never silently deleted.

There is no automatic cleanup of branches or worktrees, on purpose: it could
destroy an unfinished session's work.

## Before any push to GitHub (the repository is public)

- Never commit `.env`, `data/`, `*.db`, logs, `evidence/` or `.claude/`.
- Test fixtures use obviously fake credentials (`1111111111:AAFFF…`), never a
  real one.
- Scan what is being pushed:
  `git grep -nE '[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|sk-ant-|ghp_|github_pat_' <range>`
  and also check that the live values in `.env` do not appear.
- `archive/*` tags are local history, not for publication. Push branches
  explicitly; never use `git push --all` or `--tags`.
