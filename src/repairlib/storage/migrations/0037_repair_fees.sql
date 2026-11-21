-- The ticket's labour charge link was kept as maintenance rows in audit_events; it is state, not history.
-- A row with a NULL charge records that the labour amount was explicitly set to zero.
CREATE UNIQUE INDEX repair_charges_owner ON repair_charges(shop_id, repair_id, id);
CREATE TABLE repair_fees (
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    labor_charge_id INTEGER,
    PRIMARY KEY (shop_id, repair_id),
    UNIQUE (shop_id, labor_charge_id),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, repair_id, labor_charge_id) REFERENCES repair_charges(shop_id, repair_id, id)
);
INSERT INTO repair_fees(shop_id, repair_id, labor_charge_id)
SELECT e.shop_id, e.entity_id, json_extract(e.changes, '$.charge_id')
FROM audit_events AS e
WHERE e.id IN (
    SELECT max(id) FROM audit_events
    WHERE actor_kind = 'maintenance' AND operation = 'repair.fees' AND entity_type = 'repair'
      AND json_extract(changes, '$.fee') = 'labor'
    GROUP BY shop_id, entity_id
)
AND EXISTS (SELECT 1 FROM repairs AS r WHERE r.shop_id = e.shop_id AND r.id = e.entity_id)
AND (json_extract(e.changes, '$.charge_id') IS NULL OR EXISTS (
    SELECT 1 FROM repair_charges AS c
    WHERE c.shop_id = e.shop_id AND c.repair_id = e.entity_id AND c.id = json_extract(e.changes, '$.charge_id')
));
DELETE FROM audit_events WHERE actor_kind = 'maintenance' AND operation = 'repair.fees';
