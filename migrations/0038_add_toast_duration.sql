-- 0038_add_toast_duration.sql
-- Per-staff toast display duration (routes/staff.py's
-- update_own_notification_prefs, templates/base.html's Settings &
-- privacy modal). Plain column on staff, same pattern as
-- 0022_staff_notification_prefs.sql -- a personal display preference,
-- one row per staff member, no history needed. Default 4 matches the
-- previously-hardcoded TOAST_DEFAULT_DURATION_MS (4000ms) in
-- templates/base.html, so nothing changes behaviorally until a staff
-- member edits their own preference.
ALTER TABLE staff ADD COLUMN toast_duration_seconds INTEGER NOT NULL DEFAULT 4;
