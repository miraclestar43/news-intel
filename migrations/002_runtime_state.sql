-- Small key/value table for runtime toggles set via Telegram query commands
-- (e.g. /mute), so they persist across job runs without a dedicated table
-- per feature.

CREATE TABLE IF NOT EXISTS runtime_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
