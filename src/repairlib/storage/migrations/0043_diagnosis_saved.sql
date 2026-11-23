-- Tell an unsaved diagnosis fee from an explicit zero. Rows that already exist keep showing what they showed.
ALTER TABLE repair_fees ADD COLUMN diagnosis_saved INTEGER NOT NULL DEFAULT 1 CHECK (diagnosis_saved IN (0, 1));
