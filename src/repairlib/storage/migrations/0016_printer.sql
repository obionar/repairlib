ALTER TABLE shop_settings ADD COLUMN printer_driver TEXT NOT NULL DEFAULT 'none'
    CHECK (printer_driver IN ('none', 'p15'));
ALTER TABLE shop_settings ADD COLUMN printer_revision INTEGER NOT NULL DEFAULT 1
    CHECK (printer_revision > 0);

CREATE TABLE printer_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('printer.configure', 'printer.test', 'printer.label')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    repair_id INTEGER,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    CHECK ((operation = 'printer.label') = (repair_id IS NOT NULL))
);
