-- Images live under shop-N/images/<repair id>/. The folder is stored beside the
-- opaque 32-character name and must name the row's own repair.
ALTER TABLE attachments ADD COLUMN storage_folder TEXT NOT NULL DEFAULT ''
    CHECK (storage_folder IN ('', 'images/' || repair_id || '/'));
