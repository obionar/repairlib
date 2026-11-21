ALTER TABLE api_tokens ADD COLUMN preset TEXT
    CHECK (preset IS NULL OR (kind = 'agent' AND preset IN ('read-only', 'repairs-write', 'inventory-write')));

CREATE TRIGGER api_tokens_preset_immutable BEFORE UPDATE OF preset ON api_tokens
BEGIN
    SELECT RAISE(ABORT, 'a token keeps its preset');
END;
