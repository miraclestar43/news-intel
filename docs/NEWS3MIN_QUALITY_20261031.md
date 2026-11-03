# News quality branch for the 3-minute news (2026-10-31)

Branch `feature/news3min-quality`, worktree `../pi-news-intel-wt-news3min`.
- **What it is:** offline implementation and validation only.
- **Not merged.** Production `main`, its services, its timers and Telegram are unchanged.
- **Flag:** the ranking change sits behind `policy.features.public_safety_bucket`, which defaults to **off** (docs/GIT_WORKFLOW.md rule 5).
- **Data:** all replays ran on a copy of the production database, taken with an sqlite backup read through `mode=ro`.

## 1. Why the Chinese daily always fell back

**Observability.** `jobs/daily.py` now records every provider outcome of a run in `job_runs.detail_json`, under `provider_outcome`, and prints the same as one journal line. The record covers:
- the provider class;
- the result of the whole-digest call;
- the story-level provider;
- for each top story, its editorial result: `llm_validated`, or the source-grounded floor with its reason, latency and retries;
- a one-line `fallback_reason`.

Previously only `fallback_used` was stored.

**Diagnosis.** The automatic snapshot below ran 5 real stories through the existing bounded editorial layer: the configured `hermes_cli` provider, run one call at a time, with the production validator. The provider answered every time. Fallbacks came from **the validator rejecting correct Chinese output**:
- **Numbers:** IT之家 writes amounts as `32 万亿日元`, with a space before the magnitude, in a currency other than 元 or 美元. `_numbers_in` produced only a bare `32` (with the space) or nothing at all (without it). So the model's correct "32 trillion yen" was "not traceable". **Fixed**, with a test.
- **Entities:** the model names entities in English ("Microsoft", "Intel", "SanDisk") while the Chinese source says 微软/英特尔/闪迪, so the entity check fails. **Not fixed here.** It needs a bilingual alias table (for example the `entities` lists in topics_zh.yaml, mapped to Chinese names) or a prompt rule to use the source's own names. Next step.

On 10-31 the zh run had one top story and took 62 s, which fits one rejected attempt plus the validation-guided retry.

**Validated is not the same as correct (found in the same run):**
- **Binance:** the model-validated item says the FBI attributed "a hack" to North Korea and that the sources give no target. That is not what the Binance event's sources report.
- **Ebola:** the model-validated item says the source gives no death toll, but the headline says more than 5,000 died. The evidence packet seems to present the headline in a way the model does not treat as a source.

Both items must be reviewed before any model text reaches readers.

## 2. Where general news is lost

**Data:** events from the 7 days to 10-31, from the database copy.

**Collection (zh edition).** The zh edition's sources are IT之家 (1,733 items), 量子位 (66), 外交部 (5), 发改委 (3) and 国务院 (1). It has **no general Chinese or world news source**. BBC World (212 items) belongs to en_global only, so the zh pool can never contain an Ebola outbreak.

Adding sources is a source-policy decision (docs/SOURCE_POLICY.md), so it is not done on this branch. Candidates to validate include a Chinese general-news feed and the BBC Chinese service.

**Classification (both editions).** Of 1,172 en_global and 1,833 zh_cn events, 851 and 811 are `Unclassified`. Their relevance multiplier is 0.0: they average about 35 points and top out at about 50. Tech and markets average about 60.

The Ebola story was Unclassified because the world-politics gate needs a country from its vocabulary, and DR Congo is not in it.

**Fix behind the flag:** a `public_safety` bucket, category "Public Safety" (公共安全). It needs **both** a public-safety event term **and** scale:
- **Event terms:** epidemic, disaster, mass casualty or attack, in English or Chinese.
- **Scale:** a death toll of at least 10, or scale wording such as deadliest, thousands or 数千.
- **Exclusion:** titles framed as media or entertainment (game, film, anniversary).
- **When on:** materiality base 50, a death-toll magnitude bonus (+10/+18/+25 for ≥10/≥100/≥1,000), multiplier 1.30, and market-impact base 50. This mirrors world politics.

With the flag off, scoring is unchanged.

## 3. Replay on real stored events

**Run:** `python -m news_intel.jobs.news3min replay --db <copy> --as-of 2026-10-31T16:00:00-07:00`. It covers 240 events in the 24 h window, each rescored as at its own first sighting, flag off vs on.

| | Flag off | Flag on |
|---|---|---|
| "More than 5,000 die from Ebola in DR Congo's deadliest outbreak" | Unclassified, **38.8**, cannot qualify | Public Safety, **66.0**, qualifies (bar 58) |
| 129 元 GaN charger listing (production score) | 61.3 | unchanged |
| Video-game story reusing Itaewon crowd-crush footage | Unclassified | Unclassified. The first replay caught it as a false positive, and the media-title exclusion now prevents that |
| Other events | n/a | **No other score changed.** Unclassified events: 115 → 114 |

**Remaining gap.** Other general stories stay Unclassified and buried even with the flag on: the 6.3-billion-dollar Binance hack as reported by IT之家, the Haiyang nuclear plant, and the Paris protests. The 0.0 Unclassified multiplier is a policy decision for the owner.

## 4. Automatic snapshot (no human choice)

**Run:** `python -m news_intel.jobs.news3min snapshot --db <copy> --as-of … --public-safety on [--with-provider]`. It writes the newsletter's frozen JSON (schema `news3min/v0-prototype`).

**Selection:** qualified, source-linked events of both editions, by score. The picker takes one item per category first, then fills to 5 with at most two per category. Every item records its `text_source` and `fallback_reason`. The snapshot is `comparison_only` unless every item has validated Chinese text.

| Flag | Items |
|---|---|
| off | Intel/Reddit plunge · 吉利 chairman on competition · Binance covers 90% · 存储五巨头 profits · 工信部 software revenue |
| on | Intel/Reddit plunge · 吉利 chairman · Binance · **Ebola** · 存储五巨头 profits |

**With the provider:** 2 of 5 items model-validated (Binance and Ebola, both with the defects in section 1); 3 fell back on the validator defects above. **Not reader-ready.** The hand-picked prototype remains editorial direction only.

## Tests

- `tests/unit/test_news3min_quality.py`:
  - the flag defaults to off;
  - Ebola is classified with the flag and unclassified without it;
  - scale is required;
  - zh disaster vs gadget;
  - the media false positive;
  - Ebola qualifies only with the flag, and the other story is untouched;
  - the daily run records why it fell back;
  - Chinese amounts with a space or another currency are traceable.
- Full suite: 866 passed. `ruff check .` clean.
