-- Breaking-alert shadow canary: a strictly observational log of every
-- alert-decision-engine candidate/outcome, kept fully separate from
-- alert_queue (the real decision-state table) and deliveries (the real
-- Telegram send ledger). This table is written to ONLY by the shadow job
-- (jobs/alert_shadow.py); it never feeds back into eligibility, cooldown,
-- budget, or spacing decisions, and no code path ever sends a message based
-- on it. would_have_sent reflects what the existing (unmodified) decision
-- engine already decided -- this table adds persistence and a hypothetical
-- rendered message, not new decision logic.
CREATE TABLE IF NOT EXISTS alert_shadow_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at TEXT NOT NULL,
    run_kind TEXT NOT NULL, -- evaluate | merge_finalize | overnight_release
    event_id TEXT NOT NULL,
    primary_title TEXT,
    category TEXT,
    severity TEXT,
    trigger_id TEXT,
    is_statement_only INTEGER NOT NULL DEFAULT 0,
    action TEXT NOT NULL,
    reason TEXT,
    message_hash TEXT,
    eligible_at TEXT,
    would_have_sent INTEGER NOT NULL DEFAULT 0,
    hypothetical_rendered_text TEXT,
    source_urls_json TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_alert_shadow_log_event_id ON alert_shadow_log(event_id);
CREATE INDEX IF NOT EXISTS idx_alert_shadow_log_evaluated_at ON alert_shadow_log(evaluated_at);
