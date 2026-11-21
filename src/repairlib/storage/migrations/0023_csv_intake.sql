CREATE TABLE csv_previews (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    preview_id TEXT NOT NULL,
    input_sha256 TEXT NOT NULL,
    context_sha256 TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, kind),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id)
);

CREATE TABLE csv_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, kind, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id)
);
