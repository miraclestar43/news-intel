-- Daily-canary incident (2026-08-06, canary_index 1/3 fail-closed on
-- E-20260806-1681): jobs/collect.py's CONTENT_CHANGED path deletes an
-- event's event_sources link before re-clustering the updated content
-- (see jobs/collect.py's DELETE FROM event_sources), and re-clustering can
-- legitimately land the content on a *different* event (a genuine title
-- change, or -- in the E-1681 case -- a product_identity.py false-conflict
-- bug blocking a legitimate self-match). Nothing previously noticed when
-- this left the *original* event with zero remaining sources -- it stayed
-- status='developing', fully eligible for scoring-dependent selection,
-- forever frozen at its last-known (now-unbacked) score/state.
--
-- orphaned_at is NULL for a normal, source-backed event; set to the UTC
-- timestamp collect.py detected zero remaining event_sources rows for the
-- event; cleared back to NULL if the event later gains a fresh source
-- (pipeline/cluster.py's _attach_source is the only other event_sources
-- writer, so keeping this in sync has exactly two call sites total).
--
-- Selection eligibility itself does NOT rely on this column alone --
-- event_repo.qualified_events_since additionally has a live
-- EXISTS(event_sources) check, which is authoritative and immune to any
-- drift in this column. orphaned_at exists for audit/investigation
-- purposes (so a future incident can see *when* and *distinguish this
-- from* other causes without needing to reconstruct it from raw_items).
ALTER TABLE events ADD COLUMN orphaned_at TEXT;

CREATE INDEX IF NOT EXISTS idx_events_orphaned_at ON events(orphaned_at);
