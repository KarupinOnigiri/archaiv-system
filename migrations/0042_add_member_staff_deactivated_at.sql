-- 0042_add_member_staff_deactivated_at.sql
-- Extends the auto-purge feature (migrations/0041_add_archive_purge_settings.sql,
-- services/archive_purge_service.py) to cover deactivated (is_active = 0)
-- members and staff accounts, not just archived assets. Neither table
-- previously recorded *when* a row went inactive -- deactivating was a
-- plain `is_active = 0` flip with no timestamp, since nothing needed one
-- until now. Mirrors assets.deleted_at (0001_initial.sql): set the
-- moment a member/staff account is deactivated, cleared back to NULL on
-- reactivate, and read by archive_purge_service.py the same way
-- deleted_at is for assets.
--
-- Existing already-inactive rows are backfilled to *now* rather than
-- left NULL -- a NULL deactivated_at would mean "never eligible for
-- auto-purge," which is safe but would make every inactive member/staff
-- account predating this migration immune forever. Backfilling to now
-- instead starts their retention clock from when this feature shipped,
-- same "opt-in, nothing changes until an admin sets a window" safety
-- net as archive_purge_days itself defaulting to 0 (None).
ALTER TABLE members ADD COLUMN deactivated_at TIMESTAMP;
ALTER TABLE staff ADD COLUMN deactivated_at TIMESTAMP;

UPDATE members SET deactivated_at = datetime('now', 'localtime') WHERE is_active = 0;
UPDATE staff SET deactivated_at = datetime('now', 'localtime') WHERE is_active = 0;
