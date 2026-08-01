-- Initial schema for pi-news-intel. SQLite, WAL mode, foreign keys on.

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    adapter_type TEXT NOT NULL,
    tier INTEGER NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    url TEXT,
    category_hints_json TEXT,
    poll_interval_seconds INTEGER NOT NULL DEFAULT 300,
    last_cursor TEXT,
    last_success_at TEXT,
    last_attempt_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    health_state TEXT NOT NULL DEFAULT 'unknown',
    health_detail TEXT,
    config_hash TEXT
);

CREATE TABLE IF NOT EXISTS raw_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    external_id TEXT,
    canonical_url TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    summary_excerpt TEXT,
    language TEXT NOT NULL DEFAULT 'en',
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    origin_family TEXT,
    metadata_json TEXT,
    UNIQUE(source_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_raw_items_url_hash ON raw_items(url_hash);
CREATE INDEX IF NOT EXISTS idx_raw_items_content_hash ON raw_items(content_hash);
CREATE INDEX IF NOT EXISTS idx_raw_items_normalized_title ON raw_items(normalized_title);
CREATE INDEX IF NOT EXISTS idx_raw_items_published_at ON raw_items(published_at);

CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    primary_title TEXT NOT NULL,
    category TEXT,
    status TEXT NOT NULL DEFAULT 'developing',
    confirmation_state TEXT NOT NULL DEFAULT 'unverified',
    first_seen_at TEXT NOT NULL,
    last_updated_at TEXT NOT NULL,
    published_anchor_at TEXT,
    final_score REAL NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT 'S0',
    significance REAL NOT NULL DEFAULT 0,
    user_relevance REAL NOT NULL DEFAULT 0,
    market_impact REAL NOT NULL DEFAULT 0,
    confirmation_quality REAL NOT NULL DEFAULT 0,
    corroboration REAL NOT NULL DEFAULT 0,
    novelty REAL NOT NULL DEFAULT 0,
    popularity REAL NOT NULL DEFAULT 0,
    freshness REAL NOT NULL DEFAULT 0,
    noise_penalty REAL NOT NULL DEFAULT 0,
    repetition_penalty REAL NOT NULL DEFAULT 0,
    material_facts_json TEXT,
    entities_json TEXT,
    llm_enrichment_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_events_final_score ON events(final_score);
CREATE INDEX IF NOT EXISTS idx_events_last_updated ON events(last_updated_at);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);

CREATE TABLE IF NOT EXISTS event_sources (
    event_id TEXT NOT NULL REFERENCES events(id),
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
    is_primary INTEGER NOT NULL DEFAULT 0,
    supports_claims_json TEXT,
    PRIMARY KEY(event_id, raw_item_id)
);

CREATE TABLE IF NOT EXISTS event_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(id),
    observed_at TEXT NOT NULL,
    novelty_score REAL NOT NULL DEFAULT 0,
    changed_facts_json TEXT,
    previous_confirmation_state TEXT,
    new_confirmation_state TEXT
);

CREATE INDEX IF NOT EXISTS idx_event_updates_event_id ON event_updates(event_id);

CREATE TABLE IF NOT EXISTS alert_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL REFERENCES events(id),
    severity TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    reason TEXT,
    eligible_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    sent_at TEXT,
    message_hash TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_alert_queue_state ON alert_queue(state);
CREATE INDEX IF NOT EXISTS idx_alert_queue_event_id ON alert_queue(event_id);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    window_start TEXT NOT NULL,
    window_end TEXT NOT NULL,
    generation_state TEXT NOT NULL DEFAULT 'pending',
    rendered_text TEXT,
    model_used TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digest_events (
    digest_id INTEGER NOT NULL REFERENCES digests(id),
    event_id TEXT NOT NULL REFERENCES events(id),
    rank INTEGER NOT NULL,
    section TEXT NOT NULL,
    PRIMARY KEY(digest_id, event_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    chat_id_hash TEXT NOT NULL,
    kind TEXT NOT NULL,
    telegram_message_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scheduled_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT REFERENCES sources(id),
    title TEXT NOT NULL,
    category TEXT,
    scheduled_at TEXT NOT NULL,
    detail TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_scheduled_events_at ON scheduled_events(scheduled_at);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT REFERENCES events(id),
    chat_id_hash TEXT,
    verdict TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    detail_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_runs_job_name ON job_runs(job_name, started_at);

CREATE TABLE IF NOT EXISTS job_locks (
    job_name TEXT PRIMARY KEY,
    locked_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
