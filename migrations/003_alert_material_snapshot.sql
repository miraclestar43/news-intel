-- Stores the structured material-fact snapshot in effect when an alert was
-- recorded, so a later candidate for the same event can be diffed against
-- exactly what was actually sent (not just re-derived from current state).

ALTER TABLE alert_queue ADD COLUMN material_snapshot_json TEXT;
ALTER TABLE alert_queue ADD COLUMN material_version_hash TEXT;
