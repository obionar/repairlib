-- A ticket now carries two protected fees: the bench diagnosis and the repair labour.
-- Rebuild the table so both charge links keep their composite foreign keys.
CREATE TABLE repair_fees_new (
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    labor_charge_id INTEGER,
    diagnosis_charge_id INTEGER,
    PRIMARY KEY (shop_id, repair_id),
    UNIQUE (shop_id, labor_charge_id),
    UNIQUE (shop_id, diagnosis_charge_id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, repair_id, labor_charge_id) REFERENCES repair_charges(shop_id, repair_id, id),
    FOREIGN KEY (shop_id, repair_id, diagnosis_charge_id) REFERENCES repair_charges(shop_id, repair_id, id)
);
INSERT INTO repair_fees_new(shop_id, repair_id, labor_charge_id)
SELECT shop_id, repair_id, labor_charge_id FROM repair_fees;
DROP TABLE repair_fees;
ALTER TABLE repair_fees_new RENAME TO repair_fees;
