-- 0022_staff_notification_prefs.sql
-- Per-user notification preferences, surfaced in the "Settings & privacy"
-- panel (templates/base.html's account dropdown). Distinct from the
-- app-wide due_soon_hours threshold in app_settings (0018/0019 --
-- routes/staff.py's manage-notification-settings endpoints, admin-only,
-- controls WHEN a due-soon alert is generated at all) -- these three
-- columns instead control WHETHER a given signed-in staff member sees
-- that already-generated alert in their own bell dropdown. Plain columns
-- on staff rather than a separate table: one row per staff member, no
-- history needed, same shape as the existing photo_path/staff_code
-- additions in 0020.
--
-- Default 1 (opt-out, not opt-in) so existing accounts and the notifications
-- already sitting in the table stay visible exactly as before this
-- migration -- adding this feature must not silently mute anyone.
ALTER TABLE staff ADD COLUMN notify_due_soon INTEGER NOT NULL DEFAULT 1;
ALTER TABLE staff ADD COLUMN notify_overdue INTEGER NOT NULL DEFAULT 1;
ALTER TABLE staff ADD COLUMN notify_requests INTEGER NOT NULL DEFAULT 1;
