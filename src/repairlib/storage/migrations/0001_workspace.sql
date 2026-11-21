CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL
);

CREATE TABLE shops (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0)
);

CREATE TABLE shop_settings (
    shop_id INTEGER NOT NULL PRIMARY KEY REFERENCES shops(id),
    timezone TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)
);

CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    username TEXT NOT NULL CHECK (length(trim(username)) > 0),
    role TEXT NOT NULL CHECK (role IN ('admin', 'technician', 'viewer')),
    password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    UNIQUE (shop_id, id),
    UNIQUE (shop_id, username)
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('maintenance', 'user')),
    actor_user_id INTEGER,
    operation TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    FOREIGN KEY (shop_id, actor_user_id) REFERENCES users(shop_id, id),
    CHECK ((actor_kind = 'maintenance' AND actor_user_id IS NULL)
        OR (actor_kind = 'user' AND actor_user_id IS NOT NULL))
);
