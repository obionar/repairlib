ALTER TABLE api_tokens ADD COLUMN kind TEXT NOT NULL DEFAULT 'user' CHECK (kind IN ('user', 'agent'));

CREATE TRIGGER api_tokens_kind_marks_name BEFORE INSERT ON api_tokens
WHEN (NEW.kind = 'agent') <> (lower(NEW.name) LIKE 'agent: %')
BEGIN
    SELECT RAISE(ABORT, 'agent tokens, and only agent tokens, are labelled "agent: "');
END;

CREATE TRIGGER api_tokens_kind_immutable BEFORE UPDATE OF kind, name ON api_tokens
BEGIN
    SELECT RAISE(ABORT, 'a token keeps its kind and name');
END;

CREATE TABLE audit_events_new (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL REFERENCES shops(id),
    actor_kind TEXT NOT NULL CHECK (actor_kind IN ('maintenance', 'user', 'agent')),
    actor_user_id INTEGER,
    operation TEXT NOT NULL,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    actor_username TEXT,
    source TEXT,
    entity_type TEXT,
    entity_id INTEGER,
    changes TEXT,
    token_label TEXT,
    FOREIGN KEY (shop_id, actor_user_id) REFERENCES users(shop_id, id),
    CHECK ((actor_kind = 'maintenance' AND actor_user_id IS NULL)
        OR (actor_kind = 'user' AND actor_user_id IS NOT NULL)
        OR (actor_kind = 'agent' AND actor_user_id IS NOT NULL AND source = 'api'
            AND lower(token_label) LIKE 'agent: %'))
);

INSERT INTO audit_events_new
    (id, shop_id, actor_kind, actor_user_id, operation, occurred_at, actor_username, source,
     entity_type, entity_id, changes, token_label)
SELECT id, shop_id, actor_kind, actor_user_id, operation, occurred_at, actor_username, source,
       entity_type, entity_id, changes, token_label
FROM audit_events;

DROP TABLE audit_events;

ALTER TABLE audit_events_new RENAME TO audit_events;

CREATE INDEX audit_customer_history ON audit_events(shop_id, entity_id, id)
    WHERE entity_type = 'customer';

CREATE INDEX audit_events_activity ON audit_events(shop_id, entity_type, entity_id, id);

CREATE TRIGGER audit_events_agent_requires_agent_token BEFORE INSERT ON audit_events
WHEN NEW.actor_kind = 'agent' AND NOT EXISTS (
    SELECT 1 FROM api_tokens AS t
    WHERE t.shop_id = NEW.shop_id AND t.user_id = NEW.actor_user_id
      AND t.name = NEW.token_label AND t.kind = 'agent')
BEGIN
    SELECT RAISE(ABORT, 'an agent audit event needs one of that user''s agent tokens');
END;

CREATE TRIGGER audit_events_mark_agent AFTER INSERT ON audit_events
WHEN NEW.actor_kind = 'user' AND NEW.source = 'api' AND EXISTS (
    SELECT 1 FROM api_tokens AS t
    WHERE t.shop_id = NEW.shop_id AND t.user_id = NEW.actor_user_id
      AND t.name = NEW.token_label AND t.kind = 'agent')
BEGIN
    UPDATE audit_events SET actor_kind = 'agent' WHERE id = NEW.id;
END;
