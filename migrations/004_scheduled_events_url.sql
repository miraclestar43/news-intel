-- Calendar claims in the weekly brief must map back to an official URL,
-- not just a title/date.
ALTER TABLE scheduled_events ADD COLUMN url TEXT;
