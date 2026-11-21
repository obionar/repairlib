CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 160),
    phone TEXT NOT NULL DEFAULT '' CHECK (length(phone) <= 80),
    email TEXT NOT NULL DEFAULT '' CHECK (length(email) <= 254),
    address TEXT NOT NULL DEFAULT '' CHECK (length(address) <= 500),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id)
);

CREATE TABLE customer_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL,
    request_key TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    customer_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, customer_id) REFERENCES customers(shop_id, id)
);

ALTER TABLE audit_events ADD COLUMN token_label TEXT;
