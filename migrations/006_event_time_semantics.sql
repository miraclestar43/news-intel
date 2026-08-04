-- Gate A remediation Phase 2: separate ingestion time from validated
-- source/event time. published_anchor_at may now be NULL (undated or
-- future/malformed at ingestion -> archive_only) instead of always
-- defaulting to the ingestion instant. content_changed_at is bumped only
-- when an existing event receives a genuinely new/changed raw item.
ALTER TABLE events ADD COLUMN content_changed_at TEXT;
ALTER TABLE events ADD COLUMN archive_only INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_events_published_anchor ON events(published_anchor_at);
CREATE INDEX IF NOT EXISTS idx_events_content_changed ON events(content_changed_at);
