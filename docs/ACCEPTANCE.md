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
