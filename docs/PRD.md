# Pi News Intelligence — Product Requirements (Phase 0/1 summary)

Full source-of-truth is the implementation packet the user supplied
(`Pi_News_Intelligence_Implementation_Packet.md`). This file summarizes what
Phase 1 actually implements; sections not listed here (breaking alerts,
weekly brief, Telegram query commands, feedback tuning) are Phase 2/3/4 and
are represented only as prepared-but-unenabled systemd units and an untouched
data model (`alert_queue`, `scheduled_events`, `feedback`).

## What ships in Phase 1

- Collection every 5 minutes from ~20 configured sources (RSS/Atom, Federal
  Register JSON, HN JSON, ICS calendars, GDELT DOC API, config-driven HTML
  index pages for gov.cn/PBOC, and disabled social-mirror stubs).
- Normalization, exact dedupe (external ID / canonical URL / content hash),
  and event clustering (title similarity, trigram+entity overlap).
- Deterministic confirmation-state machine (unverified → reported → confirmed
  → official, disputed sticky) driven purely by source tier/origin-family,
  never by the LLM.
- Rule scoring (`pipeline/scoring.py`) producing the weighted `final_score`
  and a data-model-only S0-S3 tag (real alert delivery gate is Phase 2).
- MiMo/LLM boundary (`llm/provider.py`, `llm/openai_compatible.py`) with a
  documented env-var contract and a deterministic fallback
  (`llm/fallback.py`) that keeps the daily brief useful if the LLM is down.
- Telegram rendering/splitting/allowlist/idempotent delivery
  (`delivery/telegram.py`), defaulting to dry-run until a bot token/chat id
  are configured and confirmed.
- `pi-news doctor|status|collect|digest daily` CLI; `pi-news-collect` and
  `pi-news-daily` systemd user units prepared (collect enabled-ready,
  daily left in `--dry-run`); weekly/held/cleanup/health/bot units are
  present as clearly-labeled placeholders for later phases.

## Known Phase 1 limitations

- `gov_cn_policy` / `pboc_policy` HTML selectors were written from the design
  doc's description, not a live inspection of the current page markup — they
  currently report `degraded` (selector matched nothing) rather than crashing,
  which is the intended isolation behavior, but the selectors need a real fix
  before those two sources are useful.
- Rule-based scoring has no LLM-assisted semantic diversity check yet, so a
  single prolific T0 source (e.g. NVIDIA's developer blog) can currently
  dominate Top 3/Quick Briefs on a given run. The soft coverage preference
  (design doc 3.4) is enforced by category multipliers only, not yet by an
  explicit per-category cap — a candidate for Phase 4 tuning.
- Entity extraction is a deterministic known-vocabulary + capitalized-phrase
  heuristic, not NLP — good enough to drive clustering/scoring but not a
  general-purpose entity recognizer.
