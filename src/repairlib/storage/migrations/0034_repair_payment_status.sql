ALTER TABLE repairs ADD COLUMN payment_paid INTEGER CHECK (payment_paid IN (0, 1));
