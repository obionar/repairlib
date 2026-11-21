CREATE TABLE estimates (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'accepted', 'superseded')),
    note TEXT NOT NULL DEFAULT '' CHECK (length(note) <= 1000),
    parts_subtotal_cents INTEGER NOT NULL CHECK (parts_subtotal_cents >= 0),
    labor_subtotal_cents INTEGER NOT NULL CHECK (labor_subtotal_cents >= 0),
    subtotal_cents INTEGER NOT NULL CHECK (subtotal_cents = parts_subtotal_cents + labor_subtotal_cents),
    discount_kind TEXT NOT NULL CHECK (discount_kind IN ('none', 'amount', 'percent')),
    discount_value INTEGER NOT NULL CHECK (discount_value >= 0),
    discount_cents INTEGER NOT NULL CHECK (discount_cents BETWEEN 0 AND subtotal_cents),
    total_cents INTEGER NOT NULL CHECK (total_cents = subtotal_cents - discount_cents),
    created_by INTEGER NOT NULL,
    created_by_username TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    accepted_by INTEGER,
    accepted_by_username TEXT,
    accepted_at TEXT,
    UNIQUE (shop_id, id),
    UNIQUE (shop_id, repair_id, version),
    FOREIGN KEY (shop_id, repair_id) REFERENCES repairs(shop_id, id),
    FOREIGN KEY (shop_id, created_by) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, accepted_by) REFERENCES users(shop_id, id),
    CHECK ((status = 'draft') = (accepted_at IS NULL))
);

CREATE UNIQUE INDEX estimates_one_accepted ON estimates(shop_id, repair_id) WHERE status = 'accepted';

CREATE TABLE estimate_lines (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    estimate_id INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position > 0),
    kind TEXT NOT NULL CHECK (kind IN ('part', 'labor')),
    description TEXT NOT NULL,
    quantity_milli INTEGER NOT NULL CHECK (quantity_milli > 0),
    unit TEXT NOT NULL,
    unit_price_cents INTEGER NOT NULL CHECK (unit_price_cents >= 0),
    line_total_cents INTEGER NOT NULL CHECK (line_total_cents >= 0),
    UNIQUE (shop_id, estimate_id, position),
    FOREIGN KEY (shop_id, estimate_id) REFERENCES estimates(shop_id, id)
);

CREATE TRIGGER estimate_lines_immutable_update BEFORE UPDATE ON estimate_lines
BEGIN
    SELECT RAISE(ABORT, 'estimate lines are immutable');
END;

CREATE TRIGGER estimate_lines_immutable_delete BEFORE DELETE ON estimate_lines
BEGIN
    SELECT RAISE(ABORT, 'estimate lines are immutable');
END;

CREATE TRIGGER estimates_immutable_delete BEFORE DELETE ON estimates
BEGIN
    SELECT RAISE(ABORT, 'estimates are immutable');
END;

CREATE TRIGGER estimates_immutable_snapshot BEFORE UPDATE ON estimates
WHEN NEW.repair_id IS NOT OLD.repair_id OR NEW.version IS NOT OLD.version
    OR NEW.note IS NOT OLD.note OR NEW.parts_subtotal_cents IS NOT OLD.parts_subtotal_cents
    OR NEW.labor_subtotal_cents IS NOT OLD.labor_subtotal_cents
    OR NEW.subtotal_cents IS NOT OLD.subtotal_cents OR NEW.discount_kind IS NOT OLD.discount_kind
    OR NEW.discount_value IS NOT OLD.discount_value OR NEW.discount_cents IS NOT OLD.discount_cents
    OR NEW.total_cents IS NOT OLD.total_cents OR NEW.created_by IS NOT OLD.created_by
    OR NEW.created_at IS NOT OLD.created_at OR NEW.shop_id IS NOT OLD.shop_id
    OR NOT ((OLD.status = 'draft' AND NEW.status = 'accepted')
            OR (OLD.status = 'accepted' AND NEW.status = 'superseded'))
BEGIN
    SELECT RAISE(ABORT, 'estimate snapshots are immutable');
END;

CREATE TABLE estimate_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('estimate.create', 'estimate.accept')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    estimate_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, estimate_id) REFERENCES estimates(shop_id, id)
);
