ALTER TABLE customers ADD COLUMN archived_at TEXT;

CREATE INDEX audit_customer_history ON audit_events(shop_id, entity_id, id)
    WHERE entity_type = 'customer';
