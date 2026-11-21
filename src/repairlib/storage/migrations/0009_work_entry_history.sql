ALTER TABLE work_entries ADD COLUMN revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0);

ALTER TABLE work_entries ADD COLUMN deleted_at TEXT;

CREATE TABLE work_entry_revisions (
    id INTEGER PRIMARY KEY,
    shop_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    kind TEXT NOT NULL CHECK (kind IN ('work', 'test', 'contact', 'handover', 'note')),
    body TEXT NOT NULL,
    editor_id INTEGER NOT NULL,
    editor_username TEXT NOT NULL,
    reason TEXT NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0 CHECK (deleted IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (shop_id, entry_id, revision),
    FOREIGN KEY (shop_id, entry_id) REFERENCES work_entries(shop_id, id),
    FOREIGN KEY (shop_id, editor_id) REFERENCES users(shop_id, id)
);

INSERT INTO work_entry_revisions
    (shop_id, entry_id, revision, kind, body, editor_id, editor_username, reason,
     deleted, created_at)
SELECT shop_id, id, 1, kind, body, author_id, author_username, '', 0, created_at
FROM work_entries;

CREATE INDEX work_entry_history ON work_entry_revisions(shop_id, entry_id, revision DESC);
