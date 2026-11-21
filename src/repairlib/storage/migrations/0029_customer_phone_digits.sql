ALTER TABLE customers ADD COLUMN phone_digits TEXT NOT NULL DEFAULT '';

-- Scan UTF-8 bytes to keep ASCII digits even in legacy free text.
CREATE TRIGGER customers_phone_digits_update
AFTER UPDATE OF phone ON customers
BEGIN
    UPDATE customers
    SET phone_digits = (
        WITH RECURSIVE scan(pos, digits) AS (
            VALUES (1, '')
            UNION ALL
            SELECT pos + 1, digits ||
                CASE WHEN substr(CAST(NEW.phone AS BLOB), pos, 1) BETWEEN x'30' AND x'39'
                     THEN CAST(substr(CAST(NEW.phone AS BLOB), pos, 1) AS TEXT)
                     ELSE '' END
            FROM scan WHERE pos <= length(CAST(NEW.phone AS BLOB))
        )
        SELECT digits FROM scan WHERE pos = length(CAST(NEW.phone AS BLOB)) + 1
    )
    WHERE shop_id = NEW.shop_id AND id = NEW.id;
END;

CREATE TRIGGER customers_phone_digits_insert
AFTER INSERT ON customers
BEGIN
    UPDATE customers SET phone = NEW.phone WHERE shop_id = NEW.shop_id AND id = NEW.id;
END;

-- Backfill through the same trigger used for later writes.
UPDATE customers SET phone = phone WHERE phone <> '';

CREATE INDEX customers_shop_phone_digits ON customers(shop_id, phone_digits);
