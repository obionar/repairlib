ALTER TABLE shop_settings ADD COLUMN diagnosis_fee_cents INTEGER NOT NULL DEFAULT 2000
    CHECK (diagnosis_fee_cents BETWEEN 0 AND 10000000000);
