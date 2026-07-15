-- 0003_reference_codes.sql
-- Adds:
--   assets.serial_code   — optional manufacturer serial (nullable, unique when set)
--   members.member_code  — auto-generated internal reference code (MBR-00001, ...)
--   staff.staff_code     — auto-generated internal reference code (STF-00001, ...)
--
-- SQLite's ALTER TABLE ADD COLUMN does not allow a UNIQUE constraint
-- directly, so each column is added plain and uniqueness is enforced with
-- a separate index instead. A plain UNIQUE index still allows any number
-- of NULLs, which matters here since existing rows won't have a value
-- until backfilled.

ALTER TABLE assets ADD COLUMN serial_code TEXT;
CREATE UNIQUE INDEX idx_assets_serial_code ON assets(serial_code);

ALTER TABLE members ADD COLUMN member_code TEXT;
CREATE UNIQUE INDEX idx_members_member_code ON members(member_code);

ALTER TABLE staff ADD COLUMN staff_code TEXT;
CREATE UNIQUE INDEX idx_staff_staff_code ON staff(staff_code);

-- Backfill any rows that already exist (e.g. the seeded admin account)
-- so nothing is left without a code. New rows get theirs assigned by the
-- application at insert time — see add_member_submit / add_staff_submit.
UPDATE members SET member_code = printf('MBR-%05d', id) WHERE member_code IS NULL;
UPDATE staff SET staff_code = printf('STF-%05d', id) WHERE staff_code IS NULL;
