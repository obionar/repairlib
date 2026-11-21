CREATE TABLE settings_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id)
);
