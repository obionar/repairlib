ALTER TABLE attachments ADD COLUMN caption TEXT NOT NULL DEFAULT '' CHECK (length(caption) <= 1000);

ALTER TABLE attachment_requests RENAME TO previous_attachment_requests;
CREATE TABLE attachment_requests (
    shop_id INTEGER NOT NULL,
    actor_id INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('attachment.create', 'attachment.detach', 'attachment.caption', 'attachment.link')),
    request_key TEXT NOT NULL CHECK (length(request_key) BETWEEN 1 AND 128),
    fingerprint TEXT NOT NULL,
    attachment_id INTEGER NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (shop_id, actor_id, operation, request_key),
    FOREIGN KEY (shop_id, actor_id) REFERENCES users(shop_id, id),
    FOREIGN KEY (shop_id, attachment_id) REFERENCES attachments(shop_id, id)
);
INSERT INTO attachment_requests SELECT * FROM previous_attachment_requests;
DROP TABLE previous_attachment_requests;

CREATE UNIQUE INDEX attachments_same_repair ON attachments(shop_id, repair_id, id);

CREATE TABLE attachment_links (
    shop_id INTEGER NOT NULL,
    repair_id INTEGER NOT NULL,
    attachment_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    PRIMARY KEY (shop_id, repair_id, attachment_id, entry_id),
    FOREIGN KEY (shop_id, repair_id, attachment_id) REFERENCES attachments(shop_id, repair_id, id),
    FOREIGN KEY (shop_id, repair_id, entry_id) REFERENCES work_entries(shop_id, repair_id, id)
);

CREATE INDEX attachment_links_entry ON attachment_links(shop_id, repair_id, entry_id);

INSERT INTO attachment_links (shop_id, repair_id, attachment_id, entry_id)
SELECT shop_id, repair_id, id, entry_id FROM attachments WHERE entry_id IS NOT NULL;

-- The old nullable column remains in the original table definition. All links
-- now live in attachment_links, including links made on upload.
UPDATE attachments SET entry_id = NULL WHERE entry_id IS NOT NULL;
