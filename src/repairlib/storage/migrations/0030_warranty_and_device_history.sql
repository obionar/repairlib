ALTER TABLE repairs ADD COLUMN warranty_until TEXT
    CHECK (warranty_until IS NULL OR (
        date(warranty_until, '+0 days') IS warranty_until
        AND warranty_until >= '0001-01-01'
    ));

CREATE INDEX repairs_shop_serial_history ON repairs(shop_id, serial, created_at, id);
