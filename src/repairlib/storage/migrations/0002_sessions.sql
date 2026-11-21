CREATE TABLE local_workspace (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    shop_id INTEGER NOT NULL REFERENCES shops(id)
);

INSERT INTO local_workspace(id, shop_id)
VALUES (1, (SELECT CASE WHEN count(*) = 1 THEN min(id) END FROM shops));

CREATE TABLE sessions (
    token_hash TEXT PRIMARY KEY NOT NULL,
    shop_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    user_revision INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    FOREIGN KEY (shop_id, user_id) REFERENCES users(shop_id, id)
);

CREATE INDEX sessions_user ON sessions(shop_id, user_id);
