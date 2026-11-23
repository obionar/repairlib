-- Date every replay receipt at insert. Migration 0025 left a blank default, so rows written
-- afterwards looked ancient to cleanup. Existing blank dates start their window now.
UPDATE csv_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';
UPDATE customer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';
UPDATE inventory_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';
UPDATE portable_imports SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';
UPDATE printer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';
UPDATE stock_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE created_at = '';

CREATE TRIGGER csv_requests_dated AFTER INSERT ON csv_requests WHEN NEW.created_at = ''
BEGIN UPDATE csv_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER customer_requests_dated AFTER INSERT ON customer_requests WHEN NEW.created_at = ''
BEGIN UPDATE customer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER inventory_requests_dated AFTER INSERT ON inventory_requests WHEN NEW.created_at = ''
BEGIN UPDATE inventory_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER portable_imports_dated AFTER INSERT ON portable_imports WHEN NEW.created_at = ''
BEGIN UPDATE portable_imports SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER printer_requests_dated AFTER INSERT ON printer_requests WHEN NEW.created_at = ''
BEGIN UPDATE printer_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
CREATE TRIGGER stock_requests_dated AFTER INSERT ON stock_requests WHEN NEW.created_at = ''
BEGIN UPDATE stock_requests SET created_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE rowid = NEW.rowid; END;
