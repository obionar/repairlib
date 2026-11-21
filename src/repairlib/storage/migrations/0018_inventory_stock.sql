ALTER TABLE inventory_items ADD COLUMN on_hand_milli INTEGER NOT NULL DEFAULT 0 CHECK (
    typeof(on_hand_milli) = 'integer' AND on_hand_milli BETWEEN 0 AND 1000000000000
);

CREATE TABLE inventory_movements (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    item_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('receive', 'adjust', 'reversal')),
    quantity_milli INTEGER NOT NULL CHECK (
        typeof(quantity_milli) = 'integer' AND quantity_milli <> 0
        AND quantity_milli BETWEEN -1000000000 AND 1000000000
        AND (kind <> 'receive' OR quantity_milli > 0)
    ),
    on_hand_after_milli INTEGER NOT NULL CHECK (
        typeof(on_hand_after_milli) = 'integer' AND on_hand_after_milli BETWEEN 0 AND 1000000000000
    ),
    reason TEXT NOT NULL CHECK (length(reason) <= 200 AND (kind = 'receive' OR length(reason) > 0)),
    reverses_id INTEGER CHECK ((kind = 'reversal') = (reverses_id IS NOT NULL)),
    actor_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, id),
    UNIQUE (shop_id, reverses_id),
    FOREIGN KEY (shop_id, item_id) REFERENCES inventory_items(shop_id, id),
    FOREIGN KEY (shop_id, reverses_id) REFERENCES inventory_movements(shop_id, id),
    FOREIGN KEY (shop_id, actor_user_id) REFERENCES users(shop_id, id)
);

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

CREATE TABLE stock_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('inventory.receive', 'inventory.adjust', 'inventory.reverse')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    movement_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, movement_id) REFERENCES inventory_movements(shop_id, id)
);
