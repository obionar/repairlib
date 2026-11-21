-- A device photo is an image attachment with role 'device'; the ticket shows the
-- newest one that is neither detached nor purged.
ALTER TABLE attachments ADD COLUMN role TEXT NOT NULL DEFAULT ''
    CHECK (role = '' OR (role = 'device' AND media_type <> 'application/octet-stream'));
