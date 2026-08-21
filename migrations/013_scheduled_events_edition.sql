-- 2026-08-21 repair: real production defect found via replay -- scheduled_
-- events (BLS/BEA/Fed calendar releases, all currently en_global-only
-- sources) had no edition_id at all, and jobs/daily.py's
-- _tomorrow_calendar_items() -> scheduled_repo.upcoming() had no edition
-- filter either, so the IDENTICAL "TOMORROW TO WATCH" US economic-
-- calendar item was rendered verbatim as zh_cn's "明日关注" too -- exactly
-- the "never copy an English calendar item into zh_cn merely because
-- editions share a core" defect. Same DEFAULT-'en_global' backward-
-- compatibility convention as migration 012.
ALTER TABLE scheduled_events ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';

CREATE INDEX IF NOT EXISTS idx_scheduled_events_edition ON scheduled_events(edition_id);
