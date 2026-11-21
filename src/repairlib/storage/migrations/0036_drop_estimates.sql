-- Estimates are retired (owner, 2026-11-21): repairs bill from recorded charges only.
-- Audit history that mentions estimates stays as written.
DROP TRIGGER IF EXISTS estimates_immutable_snapshot;
DROP TRIGGER IF EXISTS estimates_immutable_delete;
DROP TRIGGER IF EXISTS estimate_lines_immutable_delete;
DROP TRIGGER IF EXISTS estimate_lines_immutable_update;
DROP TABLE estimate_requests;
DROP TABLE estimate_lines;
DROP TABLE estimates;
