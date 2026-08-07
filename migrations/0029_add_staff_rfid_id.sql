-- 0029_add_staff_rfid_id.sql
-- Adds staff.rfid_id -- a physical RFID keycard/tag ID, laying the
-- groundwork for a future RFID-based access/login protocol.
--
-- Lives on staff, not members: members.barcode_id is what a person
-- borrows tools with (and what a staff account's own login barcode
-- already is, via staff.member_id -> members.barcode_id); assets.rfid_id
-- is a different, unrelated thing entirely -- tagging physical tools for
-- tracking, nothing to do with a person. This is specifically "does this
-- staff account have a keycard, and if so which one" -- a staff-only
-- concept, so it gets its own column there rather than overloading
-- either of those.
--
-- Nullable + a separate unique index (SQLite's ALTER TABLE ADD COLUMN
-- doesn't support inline UNIQUE) for the same reason serial_code/
-- member_code/staff_code did in 0003_reference_codes.sql: a plain UNIQUE
-- index still allows any number of NULLs, which matters since RFID cards
-- won't necessarily be issued to every staff account, or all at once.
--
-- Deliberately not staff-role-restricted at the schema level (any staff
-- row COULD have one) -- "admin-only for now" is a display/visibility
-- rule enforced in the application layer (see routes/staff.py and the
-- staff profile/edit templates), not a database constraint, so it's
-- simple to extend to other roles later without another migration.

ALTER TABLE staff ADD COLUMN rfid_id TEXT;
CREATE UNIQUE INDEX idx_staff_rfid_id ON staff(rfid_id);
