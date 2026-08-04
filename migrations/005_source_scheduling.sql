-- Gate A remediation Phase 1.2: persisted per-source due-scheduling, so
-- restart/replay preserves next-due and backoff state (no in-memory state).
ALTER TABLE sources ADD COLUMN next_due_at TEXT;
