CREATE TABLE inventory_requests_new (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN
        ('inventory.create', 'inventory.edit', 'inventory.archive', 'inventory.restore')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, item_id) REFERENCES inventory_items(shop_id, id)
);

INSERT INTO inventory_requests_new (shop_id, actor_id, operation, request_key, fingerprint, item_id, result)
SELECT shop_id, actor_id, operation, request_key, fingerprint, item_id, result FROM inventory_requests;

DROP TABLE inventory_requests;
ALTER TABLE inventory_requests_new RENAME TO inventory_requests;
