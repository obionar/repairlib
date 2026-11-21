CREATE UNIQUE INDEX work_entries_same_repair ON work_entries(shop_id, repair_id, id);

CREATE TABLE attachments (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    entry_id INTEGER,
    uploader_id INTEGER NOT NULL,
    uploader_username TEXT NOT NULL,
    filename TEXT NOT NULL CHECK (length(filename) BETWEEN 1 AND 255),
    media_type TEXT NOT NULL CHECK (media_type IN
        ('application/octet-stream', 'image/png', 'image/jpeg', 'image/gif', 'image/webp')),
    size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
    storage_name TEXT NOT NULL UNIQUE CHECK (length(storage_name) = 32),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    detached_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, repair_id, entry_id) REFERENCES work_entries(shop_id, repair_id, id),
    FOREIGN KEY (shop_id, uploader_id) REFERENCES users(shop_id, id)
);

CREATE INDEX attachments_repair ON attachments(shop_id, repair_id, id DESC);

CREATE TABLE attachment_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('attachment.create', 'attachment.detach')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    attachment_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, attachment_id) REFERENCES attachments(shop_id, id)
);
