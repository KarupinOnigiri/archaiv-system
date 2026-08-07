-- 0025_notification_clearing.sql
-- Notifications used to disappear on their own once the underlying loan
-- was resolved (returned, or renewed out of the due-soon window) -- see
-- the old _stale_notification_ids() in services/notification_service.py.
-- Per feedback, that's the wrong model: a notification (especially an
-- overdue one) should stay visible exactly as-is until a staff member
-- deliberately clears it, the same way Facebook notifications don't
-- vanish just because the underlying post got deleted -- clearing is now
-- the *only* thing that removes a notification from view.
--
-- Soft-delete flag rather than an actual DELETE: matches the is_read
-- column's own pattern (state on the row, not row removal), and keeps
-- the option open to later add a "view cleared" archive without needing
-- another migration.
ALTER TABLE notifications ADD COLUMN is_cleared INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN cleared_at TIMESTAMP;

CREATE INDEX idx_notifications_is_cleared ON notifications(is_cleared);
