CREATE TABLE printer_requests_new (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('printer.configure', 'printer.test', 'printer.label', 'printer.inventory_label')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    repair_id INTEGER,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    CHECK ((operation = 'printer.label') = (repair_id IS NOT NULL))
);
INSERT INTO printer_requests_new SELECT shop_id, actor_id, operation, request_key, fingerprint, repair_id, result, created_at FROM printer_requests;
DROP TABLE printer_requests;
ALTER TABLE printer_requests_new RENAME TO printer_requests;
