-- Track the last authenticated request so shared-PC sessions expire when idle.
ALTER TABLE sessions ADD COLUMN last_seen_at INTEGER NOT NULL DEFAULT 0;
UPDATE sessions SET last_seen_at = CAST(strftime('%s', 'now') AS INTEGER);
