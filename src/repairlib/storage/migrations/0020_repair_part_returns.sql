-- Allow multiple partial returns against one consumption. Generic reversals stay one-to-one.
-- Preserve movement IDs, charge links and all retry receipts.
CREATE TABLE inventory_movements_next (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('receive', 'adjust', 'reversal', 'consume', 'return')),
    quantity_milli INTEGER NOT NULL CHECK (
        typeof(quantity_milli) = 'integer' AND quantity_milli <> 0
        AND quantity_milli BETWEEN -1000000000 AND 1000000000
        AND (kind NOT IN ('receive', 'return') OR quantity_milli > 0)
        AND (kind <> 'consume' OR quantity_milli < 0)
    ),
    on_hand_after_milli INTEGER NOT NULL CHECK (
        typeof(on_hand_after_milli) = 'integer' AND on_hand_after_milli BETWEEN 0 AND 1000000000000
    ),
    reason TEXT NOT NULL CHECK (length(reason) <= 200 AND (kind IN ('receive', 'consume') OR length(reason) > 0)),
    reverses_id INTEGER CHECK ((kind IN ('reversal', 'return')) = (reverses_id IS NOT NULL)),
    actor_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    UNIQUE (shop_id, item_id, id),
    FOREIGN KEY (shop_id, item_id) REFERENCES inventory_items(shop_id, id),
    FOREIGN KEY (shop_id, item_id, reverses_id) REFERENCES inventory_movements_next(shop_id, item_id, id),
    FOREIGN KEY (shop_id, actor_user_id) REFERENCES users(shop_id, id)
);

CREATE TABLE stock_requests_next (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN
        ('inventory.receive', 'inventory.adjust', 'inventory.reverse', 'inventory.consume',
         'inventory.return', 'inventory.intake_mistake')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    movement_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, movement_id) REFERENCES inventory_movements_next(shop_id, id)
);

INSERT INTO inventory_movements_next
    (id, shop_id, item_id, kind, quantity_milli, on_hand_after_milli, reason, reverses_id,
     actor_user_id, created_at)
SELECT id, shop_id, item_id, kind, quantity_milli, on_hand_after_milli, reason, reverses_id,
       actor_user_id, created_at
FROM inventory_movements;

INSERT INTO stock_requests_next (shop_id, actor_id, operation, request_key, fingerprint, movement_id, result)
SELECT shop_id, actor_id, operation, request_key, fingerprint, movement_id, result FROM stock_requests;

CREATE TABLE repair_part_consumptions_next (
    shop_id INTEGER NOT NULL,
    charge_id INTEGER NOT NULL,
    movement_id INTEGER NOT NULL,
    PRIMARY KEY (shop_id, charge_id),
    UNIQUE (shop_id, movement_id),
    FOREIGN KEY (shop_id, charge_id) REFERENCES repair_charges(shop_id, id),
    FOREIGN KEY (shop_id, movement_id) REFERENCES inventory_movements_next(shop_id, id)
);

INSERT INTO repair_part_consumptions_next (shop_id, charge_id, movement_id)
SELECT shop_id, charge_id, movement_id FROM repair_part_consumptions;

DROP TRIGGER repair_part_consumptions_append_only_delete;
DROP TABLE repair_part_consumptions;
DROP TRIGGER inventory_items_unit_locked;
DROP TRIGGER inventory_movements_append_only_delete;
DROP TABLE stock_requests;
-- Delete the former ledger as one table, including its self-referencing reversals.
DROP TABLE inventory_movements;

ALTER TABLE inventory_movements_next RENAME TO inventory_movements;
ALTER TABLE stock_requests_next RENAME TO stock_requests;
ALTER TABLE repair_part_consumptions_next RENAME TO repair_part_consumptions;

CREATE UNIQUE INDEX inventory_movements_single_reversal
    ON inventory_movements(shop_id, reverses_id) WHERE kind = 'reversal';
CREATE INDEX inventory_movements_returns
    ON inventory_movements(shop_id, reverses_id) WHERE kind = 'return';

CREATE INDEX inventory_movements_item ON inventory_movements(shop_id, item_id, id);

CREATE TRIGGER inventory_movements_append_only_update BEFORE UPDATE ON inventory_movements
BEGIN
    SELECT RAISE(ABORT, 'inventory movements are append-only');
END;

CREATE TRIGGER inventory_movements_append_only_delete BEFORE DELETE ON inventory_movements
BEGIN
    SELECT RAISE(ABORT, 'inventory movements are append-only');
END;

CREATE TRIGGER inventory_items_unit_locked BEFORE UPDATE OF unit ON inventory_items
WHEN NEW.unit IS NOT OLD.unit AND EXISTS (
    SELECT 1 FROM inventory_movements WHERE shop_id = OLD.shop_id AND item_id = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'inventory unit is fixed once stock movements exist');
END;

CREATE TRIGGER repair_part_consumptions_append_only_update BEFORE UPDATE ON repair_part_consumptions
BEGIN
    SELECT RAISE(ABORT, 'repair part consumption links are append-only');
END;

CREATE TRIGGER repair_part_consumptions_append_only_delete BEFORE DELETE ON repair_part_consumptions
BEGIN
    SELECT RAISE(ABORT, 'repair part consumption links are append-only');
END;
