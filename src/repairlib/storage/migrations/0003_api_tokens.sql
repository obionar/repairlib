CREATE TABLE api_tokens (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_revision INTEGER NOT NULL CHECK (user_revision > 0),
    token_hash TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    scope TEXT NOT NULL CHECK (scope IN ('read', 'write')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    create_key TEXT NOT NULL,
    revoke_key TEXT,
    revoked_at INTEGER,
    FOREIGN KEY (shop_id, user_id) REFERENCES users(shop_id, id),
    UNIQUE (shop_id, user_id, create_key),
    UNIQUE (shop_id, user_id, revoke_key),
    CHECK ((revoke_key IS NULL) = (revoked_at IS NULL))
);

CREATE INDEX api_tokens_owner ON api_tokens(shop_id, user_id, id);

ALTER TABLE audit_events ADD COLUMN actor_username TEXT;
ALTER TABLE audit_events ADD COLUMN source TEXT;
ALTER TABLE audit_events ADD COLUMN entity_type TEXT;
ALTER TABLE audit_events ADD COLUMN entity_id INTEGER;
ALTER TABLE audit_events ADD COLUMN changes TEXT;
