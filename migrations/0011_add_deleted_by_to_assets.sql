-- Adds deleted_by so the archived view can show who deleted each item,
-- alongside the existing deleted_at. Nullable and unset for anything
-- already archived before this migration ran -- those rows just won't
-- show a "by ..." line, same as any other missing/unknown value.
ALTER TABLE assets ADD COLUMN deleted_by INTEGER REFERENCES staff(id);
