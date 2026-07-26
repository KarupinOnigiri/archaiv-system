-- One-time migration: adds the column that stores each department's custom
-- display order, and backfills it so existing departments keep their
-- current (alphabetical) order until someone reorders them in the
-- Manage Departments modal.
--
-- Run this once against your database before deploying the updated
-- members.py -- every "ORDER BY sort_order" query will otherwise sort
-- entirely by NULLs (SQLite puts NULL first in ASC order), effectively
-- randomizing department order everywhere until this backfill runs.

ALTER TABLE departments ADD COLUMN sort_order INTEGER;

-- Backfill: give every existing department a sort_order matching its
-- current alphabetical position (0, 1, 2, ...), so nothing visually
-- jumps around the first time this ships.
UPDATE departments
SET sort_order = (
    SELECT COUNT(*)
    FROM departments AS d2
    WHERE d2.name < departments.name
);
