-- 0040_add_auto_backup_settings.sql
-- Admin-editable controls (System page) for the in-process automatic
-- database backup added alongside this migration (see app.py's
-- start_auto_backup_scheduler / services/backup_service.py) -- whether
-- it runs at all, and how many hours between runs. backup_db.py's
-- standalone CLI script (for an external cron/systemd timer) is
-- untouched and still works independently of this; this just lets the
-- app back itself up without depending on that being set up on every
-- deployment.
--
-- Seeded enabled ('1') by default -- a toolroom's database is exactly
-- the kind of single-file, no-DBA, easy-to-forget-to-back-up setup
-- where "off unless someone remembers to turn it on" would mean most
-- installs just never get backed up at all. 24 hours between runs is a
-- reasonable default cadence for a low-write-volume school inventory
-- system; both are editable from System page without touching code.
INSERT INTO app_settings (key, value) VALUES
    ('auto_backup_enabled', '1'),
    ('auto_backup_interval_hours', '24');
