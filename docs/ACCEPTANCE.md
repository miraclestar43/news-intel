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

### Known Phase 2 simplifications (documented, not silently skipped)

- The 10-minute "burst merge" of unrelated S2 candidates into one combined
  message is not implemented; only the 30-minute minimum-spacing hold is
  enforced. A near-simultaneous second S2 is held, not merged.
- Material-update detection only considers confirmation-state transitions
  (official/confirmed/reported changes already logged by
  `pipeline/confirmation.py`), not the full material_facts diff (amount,
  effective date, reversal, etc.) described in section 9 — that requires
  LLM-proposed structured facts, deferred to when MiMo enrichment is wired
  into the alert path.
- Telegram query commands are implemented and unit-tested as pure functions
  but are not yet wired into a running `python-telegram-bot` long-polling
  Application; `pi-news-bot.service` stays an unimplemented placeholder.
