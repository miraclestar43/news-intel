-- 2026-08-17: explicit multipart tracking for the weekly brief, which may
-- (rarely) exceed Telegram's 4096-character limit even after
-- delivery/weekly_formatter.py's split_weekly() targets at most two parts.
--
-- The existing `deliveries` table already gives each part its own
-- idempotency-protected row (jobs/weekly.py keys each part as
-- "<idempotency_key>:<i>of<n>"), which already prevents resending a
-- successfully-sent part on a repeated invocation -- that mechanism is
-- UNCHANGED by this migration. What was missing, and what this table adds,
-- is a first-class "one logical weekly delivery" record: ordered parts,
-- per-part text hash (not stored anywhere before), and a queryable overall
-- status (pending/partial/sent/failed) without having to reconstruct it by
-- parsing idempotency_key prefixes across multiple `deliveries` rows.
CREATE TABLE IF NOT EXISTS weekly_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,  -- weekly:<ISO-week>:<chat-hash>
    chat_id_hash TEXT NOT NULL,
    week_label TEXT NOT NULL,
    total_parts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | partial | sent | failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS weekly_delivery_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekly_delivery_id INTEGER NOT NULL REFERENCES weekly_deliveries(id),
    part_index INTEGER NOT NULL,  -- 1-based
    text_hash TEXT NOT NULL,
    telegram_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'not_attempted',  -- not_attempted | dry_run | sent | failed
    attempted_at TEXT,
    UNIQUE(weekly_delivery_id, part_index)
);

CREATE INDEX IF NOT EXISTS idx_weekly_delivery_parts_delivery ON weekly_delivery_parts(weekly_delivery_id);
