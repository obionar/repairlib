ALTER TABLE shop_settings ADD COLUMN max_upload_mib INTEGER NOT NULL DEFAULT 256
    CHECK (max_upload_mib BETWEEN 1 AND 2048);

ALTER TABLE shop_settings ADD COLUMN inline_preview_mib INTEGER NOT NULL DEFAULT 8
    CHECK (inline_preview_mib BETWEEN 1 AND 64);

ALTER TABLE attachments ADD COLUMN purged_at TEXT;

ALTER TABLE attachments ADD COLUMN purged_by_username TEXT;
