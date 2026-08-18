-- 2026-08-18: monthly intelligence brief, same safe-multipart-delivery
-- shape as migrations/011_weekly_multipart_delivery.sql (one logical
-- delivery, ordered parts, per-part text hash/attempt-state/Telegram-
-- message-id, no automatic retry). A separate table pair rather than
-- reusing weekly_deliveries directly -- the two kinds' idempotency-key
-- namespaces and window semantics are distinct (ISO week vs. calendar
-- month), and unifying them behind one edition/kind-aware table is exactly
-- what the later multi-edition migration does; duplicating the proven
-- weekly shape now keeps this migration reviewable on its own.
CREATE TABLE IF NOT EXISTS monthly_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,  -- monthly:<YYYY-MM>:<chat-hash>
    chat_id_hash TEXT NOT NULL,
    month_label TEXT NOT NULL,
    total_parts INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | partial | sent | failed
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS monthly_delivery_parts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monthly_delivery_id INTEGER NOT NULL REFERENCES monthly_deliveries(id),
    part_index INTEGER NOT NULL,  -- 1-based
    text_hash TEXT NOT NULL,
    telegram_message_id TEXT,
    status TEXT NOT NULL DEFAULT 'not_attempted',  -- not_attempted | dry_run | sent | failed
    attempted_at TEXT,
    UNIQUE(monthly_delivery_id, part_index)
);

CREATE INDEX IF NOT EXISTS idx_monthly_delivery_parts_delivery ON monthly_delivery_parts(monthly_delivery_id);
