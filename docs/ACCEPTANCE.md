# Phase 1 acceptance evidence

Ran on the Pi (`raspberrypi51`), Python 3.11.2, from `~/pi-news-intel`.

| # | Acceptance criterion | Evidence |
|---|---|---|
| 1 | Deterministic fixtures cover >=12 sources, English + Chinese | `tests/unit/test_adapters_fixtures.py` (7 adapter types), `tests/unit/test_source_manifest.py` (asserts >=12 configured sources); Chinese via `html_index_gov_cn.html` / `html_index_pboc.html` fixtures |
| 2 | Two differently-titled articles about one event cluster; unrelated same-company stories don't | `tests/integration/test_dedupe_and_cluster.py::test_two_titles_about_one_event_cluster` / `::test_unrelated_same_company_stories_stay_separate` |
| 3 | Re-running collection is idempotent | `tests/integration/test_dedupe_and_cluster.py::test_rerunning_collection_is_idempotent`; also verified live: a second real `pi-news collect --no-dry-run` pass does not duplicate `raw_items` (UNIQUE + hash-based dedupe) |
| 4 | Daily brief has 4-8 qualified events, Top 3, Chinese takeaways, sources, Tomorrow to Watch | `tests/integration/test_daily_job.py::test_daily_brief_has_qualified_events_and_chinese_takeaways` (fake LLM provider) |
| 5 | Target under 3,800 chars; splits at section boundaries if longer | `tests/integration/test_daily_job.py::test_daily_brief_stays_under_soft_char_target_or_splits`; `tests/unit/test_splitter.py` |
| 6 | Simulated MiMo outage produces a useful fallback digest | `tests/integration/test_daily_job.py::test_daily_brief_falls_back_when_llm_unavailable`; also true live (no `LLM_*` configured yet) — see rendered brief in the Phase 1 report |
| 7 | Simulated failure of several sources does not abort the job | `tests/unit/test_collect_job_isolation.py`; also observed live (GDELT 429/SSL timeout, gov.cn/PBOC selector mismatch) — job completed, other 14 sources succeeded |
| 8 | DST tests verify 10 PM Pacific across March/November transitions | `tests/unit/test_dst_schedule.py` |
| 9 | Running the same send twice creates one Telegram delivery | `tests/integration/test_daily_job.py::test_repeated_send_creates_one_delivery` |
| 10 | Telegram dry-run by default; real send only after configuration | `delivery/telegram.py` defaults `dry_run=True`; CLI `digest daily` defaults `--dry-run`; no token configured yet |
| 11 | `pytest` and `ruff check` pass | 53 passed, `ruff check .` → All checks passed |

Secret redaction: `tests/unit/test_redaction.py` (token/bearer/api_key
patterns, `Secrets.__repr__`/`__str__` never leak values).

## Phase 2 acceptance evidence (breaking alerts)

Deterministic two-key eligibility gate (`pipeline/alerts.py`), quiet-hours
held queue with consolidated overnight release (`jobs/alerts.py`), daily S2
budget/spacing/cooldown, material-update handling, and Telegram query
commands (`delivery/commands.py`) as pure functions. All real Telegram
sending remains disabled/dry-run; `pi-news-bot.service` (long-polling) is
still not implemented/started.

| # | Required scenario | Test |
|---|---|---|
| 1 | Official export control + corroborating report → one S2 alert | `test_phase2_alert_scenarios.py::test_scenario_1_...` |
| 2 | Mirror alone → unverified candidate, no push | `::test_scenario_2_...` |
| 3 | Verified statement without policy doc → labeled statement | `::test_scenario_3_...` |
| 4 | S2 at 1AM PT held; S3 at 1AM PT immediate | `::test_scenario_4_...` |
| 5 | Duplicate articles → one alert | `::test_scenario_5_...` |
| 6 | Cosmetic follow-up → no second alert | `::test_scenario_6_...` |
| 7 | Formal confirmation after statement → material update allowed | `::test_scenario_7_...` |
| 8 | Fourth S2 in a day → deferred to digest | `::test_scenario_8_...` |

Supporting coverage: quiet-hours wraparound arithmetic, independent
statement-language detection, consolidated overnight release (shared
message_hash counts as one message toward budget), `/why`, `/sources`,
`/mute` (suppresses S2, not S3), `/now`, `/tech|/market|/china|/us|/world` —
all in `tests/unit/test_alert_policy_and_commands.py`.

70 tests passing total, `ruff check .` clean.

### Phase 2 completion (burst merge, material facts, bot wiring)

All three items flagged as outstanding after the initial Phase 2 pass are
now implemented:

1. **S2 burst merge** (`jobs/alerts.py`): every S2 candidate opens or joins
   a `pending_merge` batch with a fixed 10-minute window from the first
   candidate's arrival; the batch finalizes (lazily, via
   `finalize_expired_merge_batches`) into one consolidated "Breaking Brief"
   that preserves every event id and its sources, and counts as one message
   toward the daily S2 budget (`alert_queue_repo.count_sent_s2_on_pt_date`
   groups by `message_hash`). S3 always bypasses it; quiet-hours logic is
   untouched (checked before the merge branch); candidates outside the
   window fall through to the existing 30-minute spacing rule. Tests:
   `tests/unit/test_burst_merge.py` (4 required scenarios).

2. **Structured material-fact snapshot + diff** (`pipeline/material_facts.py`):
   a `MaterialFactSnapshot` (action/actor/target_entities/amount/
   effective_date/confirmation_status/reversed/trigger_severity), extracted
   with wording-stable rules (known-vocabulary entities only, trigger id
   instead of raw headline text) so paraphrasing never registers as
   material. `diff_snapshots` flags confirmation, effective-date, amount,
   new-target, actor, reversal, and S2→S3 escalation changes;
   `validate_llm_snapshot` always overwrites `confirmation_status` with the
   code-computed value and returns `None` (safe default: no repeated alert)
   on any validation failure. Snapshots are stored per sent alert
   (`alert_queue.material_snapshot_json`, migration `003`) so repeats are
   diffed against what was actually delivered, not just current state.
   Tests: `tests/unit/test_material_facts.py` (6 required cases).

3. **Telegram query commands wired to a disabled entrypoint**
   (`delivery/bot.py`, `pi-news bot dispatch`): dispatch parses real
   `telegram.Update` payloads (`Update.de_json`, no network, no token
   needed) and enforces `TELEGRAM_ALLOWED_CHAT_IDS` before running any
   command or returning any data -- an unapproved chat gets no response and
   never reaches a handler. `pi-news-bot.service` (long-polling) remains
   uninstalled/disabled; the CLI command is a one-shot dispatcher only.
   Tests: `tests/unit/test_bot_dispatch.py` (allowed vs. unapproved chat,
   all 9 required commands, state-mutation isolation).

90 tests passing total, `ruff check .` clean.

## Phase 3 acceptance evidence (weekly intelligence brief)

Trend clustering (`pipeline/trends.py`) groups related *events* (not raw
articles) within a 7-day window by category + known-entity overlap;
strength is built only from importance, confirmation quality, source-family
diversity, persistence, and material-update count -- raw article/raw_item
counts never enter the formula. `jobs/weekly.py` derives window bounds from
calendar arithmetic (the ISO Mon-Sun week containing `now`), so repeated
invocations within the same week always compute identical bounds -- the
basis for reliable digest-row idempotency independent of exact `now`
jitter. `jobs/daily.py` skips entirely on Sunday (zero digest rows, zero
job-lock/job-run bookkeeping) so the weekly brief is a true replacement,
not an addition.

| # | Required test | Test |
|---|---|---|
| 1 | Weekly synthesizes trends, not a daily concatenation | `test_weekly_job.py::test_weekly_synthesizes_trends_not_a_daily_concatenation` |
| 2 | Prior-week comparison uses two distinct PT windows | `test_trends.py::test_prior_week_window_is_a_distinct_non_overlapping_pt_window` |
| 3 | 10 syndicated articles don't beat fewer high-quality independent events | `test_trends.py::test_ten_syndicated_articles_do_not_beat_fewer_diverse_high_quality_events` |
| 4 | Noise, unresolved, confirmation changes, reversals represented | `test_weekly_job.py::test_noise_unresolved_and_disputed_are_represented` |
| 5 | Next-week items come from stored calendar records | `test_weekly_job.py::test_next_week_items_come_from_stored_calendar_records` |
| 6 | Every factual/calendar claim maps to a stored id/URL | `test_weekly_job.py::test_every_referenced_event_and_calendar_id_is_stored` |
| 7 | Repeated Sunday calls: one weekly delivery, zero daily duplicates | `test_weekly_job.py::test_sunday_scheduler_repeated_calls_create_one_weekly_and_zero_daily_duplicates` |
| 8 | DST and ISO-week boundaries deterministic | `test_trends.py::test_week_window_deterministic_across_dst_spring_forward` |
| 9 | LLM outage still produces a useful fallback weekly brief | `test_weekly_job.py::test_llm_outage_still_produces_useful_fallback_weekly_brief` |
| 10 | Delivery remains dry-run | `test_weekly_job.py::test_delivery_remains_dry_run_by_default` |

102 tests passing total, `ruff check .` clean. Verified live: `pi-news
digest weekly --dry-run` runs cleanly against the real DB; Hermes and the
polymarket bot remain active and untouched; no `pi-news` systemd units
present.

### Known Phase 3 limitations

- Trend clustering uses the same known-vocabulary + capitalized-phrase
  entity heuristic as event-level clustering (Phase 1) -- adequate for
  grouping but not a substitute for genuine topic modeling.
- The deterministic fallback's "Chinese synthesis" is a templated sentence
  referencing counts, not narrative translation -- true 150-250 character
  synthesis prose requires the LLM path (`compose_weekly.md`), which is
  fully wired but untested against a live MiMo endpoint (none configured
  yet, per Phase 1's documented limitation).
- Weekly Telegram delivery, when a chat id is eventually configured, sends
  each of the (at most two) parts as its own idempotent delivery; this is
  implemented and dry-run-tested but not yet exercised against a real bot
  token.
