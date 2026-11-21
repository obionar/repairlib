ALTER TABLE repairs ADD COLUMN warranty_repair_id INTEGER
    CHECK (warranty_repair_id IS NULL OR warranty_repair_id >= 1);

CREATE INDEX repairs_shop_warranty_return ON repairs(shop_id, warranty_repair_id, id);
