CREATE TABLE portable_imports (
    shop_id INTEGER NOT NULL PRIMARY KEY REFERENCES shops(id),
    actor_id INTEGER NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    last_movement_id INTEGER,
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, last_movement_id) REFERENCES inventory_movements(shop_id, id)
);
