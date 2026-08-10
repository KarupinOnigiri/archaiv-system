-- 0041_add_archive_purge_settings.sql
-- Admin-editable auto-purge policy (System page) for archived
-- (soft-deleted) assets -- how long an archived asset sits in the
-- Archived view before services/archive_purge_service.py's scheduled
-- job permanently removes it, mirroring how 0040 added the
-- auto-backup on/off + interval pair for services/backup_service.py.
--
-- archive_purge_days is a whole number of days measured from
-- assets.deleted_at (see routes/assets.py's delete_asset). '0' means
-- "None" -- auto-purge disabled, archived assets are kept forever
-- until someone deletes them by hand -- which is why this is seeded
-- '0' rather than some preset like 30: an existing install's archived
-- items shouldn't start silently disappearing the moment this
-- migration runs. An admin has to opt in to a retention window (1
-- week/1 month/6 months/1 year, or any custom day count) from the
-- System page before anything actually gets purged.
INSERT INTO app_settings (key, value) VALUES
    ('archive_purge_days', '0');
