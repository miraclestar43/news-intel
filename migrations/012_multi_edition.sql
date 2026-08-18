-- 2026-08-18: multi-edition architecture. Introduces `edition_id` as a
-- first-class identity across the tables that must be independent per
-- edition (candidate scoring/classification, selected events, digests,
-- deliveries, job runs), while raw_items/sources stay a technically-shared
-- pool with edition membership recorded on each row (config/sources.yaml's
-- `editions:` list decides membership at collection time; see
-- adapters -> pipeline/dedupe.py threading).
--
-- Backward compatibility: every existing row (100% English production
-- history) gets edition_id='en_global' via the DEFAULT below -- this does
-- not change any existing row's identity, timestamps, delivery status,
-- Telegram message IDs, or historical text. New idempotency keys for
-- zh_cn include the edition explicitly (daily:zh_cn:<date>:<hash>) so an
-- English and Chinese delivery can never collide merely because they
-- share a bot/recipient -- see delivery/telegram.py.
--
-- Events are clustered SEPARATELY per edition (pipeline/cluster.py scopes
-- candidate matching to same-edition raw_items only): the same underlying
-- real-world event discovered by both an English and a Chinese source
-- becomes two independent event rows, one per edition, each fully inside
-- that edition's own scoring/classification/selection pipeline. This is
-- what makes "the same material event may appear in both editions" safe
-- by construction -- there is no cross-edition merge point for a bug to
-- hide in, and a zh_cn clustering/scoring failure cannot corrupt or block
-- en_global event identity.
ALTER TABLE raw_items ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';
ALTER TABLE events ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';
ALTER TABLE digests ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';
ALTER TABLE deliveries ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';
ALTER TABLE job_runs ADD COLUMN edition_id TEXT;

CREATE INDEX IF NOT EXISTS idx_raw_items_edition ON raw_items(edition_id);
CREATE INDEX IF NOT EXISTS idx_events_edition ON events(edition_id);
CREATE INDEX IF NOT EXISTS idx_events_edition_score ON events(edition_id, final_score);
CREATE INDEX IF NOT EXISTS idx_digests_edition_kind ON digests(edition_id, kind);
CREATE INDEX IF NOT EXISTS idx_deliveries_edition ON deliveries(edition_id);

-- Generalizes migration 011's weekly-only multipart tracking to cover
-- weekly AND monthly, both editions -- `kind` distinguishes 'weekly' vs
-- 'monthly'; `edition_id` distinguishes en_global vs zh_cn. Table names
-- are kept as `weekly_deliveries`/`weekly_delivery_parts` (a naming
-- legacy from when only weekly existed) rather than renamed -- this now
-- serves both kinds, replacing the standalone monthly_deliveries/
-- monthly_delivery_parts pair this Stage-1-only branch briefly had (that
-- pair was never deployed and is superseded here, not migrated from --
-- see jobs/monthly.py's Stage 2 rewrite).
ALTER TABLE weekly_deliveries ADD COLUMN edition_id TEXT NOT NULL DEFAULT 'en_global';
ALTER TABLE weekly_deliveries ADD COLUMN kind TEXT NOT NULL DEFAULT 'weekly';
ALTER TABLE weekly_deliveries RENAME COLUMN week_label TO period_label;

CREATE INDEX IF NOT EXISTS idx_weekly_deliveries_edition_kind ON weekly_deliveries(edition_id, kind);

-- Delivery profile: separates WHICH bot/chat a delivery went to from WHICH
-- content edition it carries (task spec's "separate content edition from
-- Telegram credentials"). Both editions resolve to the same
-- delivery_profile_id ("default") in this rollout -- see
-- config/delivery_profiles.yaml -- but the column exists now so a future
-- edition-specific bot/chat requires zero migration and zero code change
-- beyond the config mapping.
ALTER TABLE deliveries ADD COLUMN delivery_profile_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE weekly_deliveries ADD COLUMN delivery_profile_id TEXT NOT NULL DEFAULT 'default';
