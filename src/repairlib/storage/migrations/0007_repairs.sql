CREATE TABLE repairs (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    customer_id INTEGER,
    technician_id INTEGER,
    device_type TEXT NOT NULL DEFAULT '' CHECK (length(device_type) <= 80),
    brand TEXT NOT NULL DEFAULT '' CHECK (length(brand) <= 80),
    model TEXT NOT NULL DEFAULT '' CHECK (length(model) <= 120),
    serial TEXT NOT NULL DEFAULT '' CHECK (length(serial) <= 120),
    display_device TEXT NOT NULL DEFAULT '' CHECK (length(display_device) <= 160),
    reported_issue TEXT NOT NULL CHECK (length(trim(reported_issue)) BETWEEN 1 AND 2000),
    notes TEXT NOT NULL DEFAULT '' CHECK (length(notes) <= 4000),
    stage TEXT NOT NULL DEFAULT 'new' CHECK (stage IN
        ('new', 'in_progress', 'waiting_parts', 'lab_test', 'customer_test', 'repaired', 'no_repair')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    FOREIGN KEY (shop_id, customer_id) REFERENCES customers(shop_id, id),
    FOREIGN KEY (shop_id, technician_id) REFERENCES users(shop_id, id)
);

CREATE INDEX repairs_queue ON repairs(shop_id, id DESC);
CREATE INDEX repairs_customer ON repairs(shop_id, customer_id, id);
CREATE INDEX repairs_technician ON repairs(shop_id, technician_id, id);

CREATE TABLE work_entries (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    author_id INTEGER NOT NULL,
    author_username TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('work', 'test', 'contact', 'handover', 'note')),
    body TEXT NOT NULL CHECK (length(trim(body)) BETWEEN 1 AND 5000),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, author_id) REFERENCES users(shop_id, id)
);

CREATE INDEX work_entries_repair ON work_entries(shop_id, repair_id, id DESC);

CREATE TABLE repair_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN
        ('repair.create', 'repair.update', 'work_entry.create')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    repair_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id)
);
