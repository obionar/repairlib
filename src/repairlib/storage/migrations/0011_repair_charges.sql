CREATE TABLE repair_charges (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('part', 'labor')),
    description TEXT NOT NULL CHECK (length(trim(description)) BETWEEN 1 AND 200),
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli BETWEEN 1 AND 1000000000),
    unit TEXT NOT NULL CHECK (length(unit) BETWEEN 1 AND 16),
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents BETWEEN 0 AND 10000000000),
    unit_cost_cents INTEGER CHECK (unit_cost_cents BETWEEN 0 AND 10000000000),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 1000),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    deleted_at TEXT,
    created_by INTEGER NOT NULL,
    created_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, created_by) REFERENCES users(shop_id, id)
);

CREATE INDEX repair_charges_repair ON repair_charges(shop_id, repair_id, id);

CREATE TABLE repair_discounts (
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('none', 'amount', 'percent')),
    value INTEGER NOT NULL CHECK (value BETWEEN 0 AND 10000000000),
    revision INTEGER NOT NULL CHECK (revision > 0),
    PRIMARY KEY (shop_id, repair_id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    CHECK (kind != 'percent' OR value <= 100),
    CHECK (kind != 'none' OR value = 0)
);

CREATE TABLE charge_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN
        ('charge.create', 'charge.update', 'repair.discount')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    repair_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id)
);
