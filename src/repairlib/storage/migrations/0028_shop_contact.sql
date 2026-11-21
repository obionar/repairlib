-- Shop contact block and handover terms for the printed repair summary.
ALTER TABLE shop_settings ADD COLUMN address TEXT NOT NULL DEFAULT '';
ALTER TABLE shop_settings ADD COLUMN phone TEXT NOT NULL DEFAULT '';
ALTER TABLE shop_settings ADD COLUMN terms TEXT NOT NULL DEFAULT '';
