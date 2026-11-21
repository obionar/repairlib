-- Age every replay receipt so cleanup can prune it after the retention window.
ALTER TABLE csv_requests ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE csv_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
ALTER TABLE customer_requests ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE customer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
ALTER TABLE inventory_requests ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE inventory_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
ALTER TABLE portable_imports ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE portable_imports SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
ALTER TABLE printer_requests ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE printer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
ALTER TABLE stock_requests ADD COLUMN created_at TEXT NOT NULL DEFAULT '';
UPDATE stock_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now');
