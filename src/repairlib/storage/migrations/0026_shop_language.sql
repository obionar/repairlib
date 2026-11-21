-- Default interface language for the workspace; empty means no default is stored.
ALTER TABLE shop_settings ADD COLUMN language TEXT NOT NULL DEFAULT '';
