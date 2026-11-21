CREATE TABLE inventory_items (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    sku TEXT NOT NULL CHECK (length(sku) BETWEEN 1 AND 80),
    description TEXT NOT NULL CHECK (length(description) BETWEEN 1 AND 200),
    category TEXT NOT NULL DEFAULT '',
    manufacturer TEXT NOT NULL DEFAULT '',
    part_number TEXT NOT NULL DEFAULT '',
    package TEXT NOT NULL DEFAULT '',
    specifications TEXT NOT NULL DEFAULT '',
    bin TEXT NOT NULL DEFAULT '',
    unit TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 16),
    low_stock_milli INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(low_stock_milli) = 'integer' AND low_stock_milli BETWEEN 0 AND 1000000000
    ),
    purchase_cost_cents INTEGER CHECK (purchase_cost_cents IS NULL OR (
        typeof(purchase_cost_cents) = 'integer' AND purchase_cost_cents BETWEEN 0 AND 10000000000
    )),
    selling_price_cents INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(selling_price_cents) = 'integer' AND selling_price_cents BETWEEN 0 AND 10000000000
    ),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    archived_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (typeof(revision) = 'integer' AND revision > 0),
    UNIQUE (shop_id, id),
    UNIQUE (shop_id, sku)
);

CREATE TABLE inventory_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('inventory.create', 'inventory.edit', 'inventory.archive')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    item_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, item_id) REFERENCES inventory_items(shop_id, id)
);
