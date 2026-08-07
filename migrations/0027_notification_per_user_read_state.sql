-- 0027_notification_per_user_read_state.sql
-- Read/cleared state used to live directly on the notifications row
-- (is_read, is_cleared/cleared_at -- see 0016_notifications.sql and
-- 0025_notification_clearing.sql). That's correct for a row targeted at
-- exactly one staff member (recipient_staff_id set -- e.g. "your
-- unrestriction request was approved"), but wrong for the two broadcast
-- kinds: a row with both recipient columns NULL (loan due/overdue
-- alerts, meant for everyone) or with recipient_role set (meant for
-- everyone with that role) is a SINGLE shared row -- one admin marking
-- it read or clearing it silently did the same for every other admin
-- looking at that identical row. Per feedback, each staff member needs
-- their own independent read/cleared state regardless of how many people
-- a given notification is visible to.
--
-- notification_reads is a per-(notification, staff) state table -- a
-- missing row means "unread, not cleared" (the default for anyone who's
-- never touched it), so nothing needs backfilling for existing
-- notifications. One real, honest side effect: the OLD shared is_read/
-- is_cleared columns on `notifications` are no longer read by the app
-- as of this migration (services/notification_service.py and
-- routes/notifications.py now compute both from this table instead) --
-- left in place rather than dropped (harmless, and dropping risks more
-- than it's worth), but anything that was marked read/cleared under the
-- old shared model will show as unread/uncleared again for everyone
-- under the new per-user model, since there's no way to know who
-- actually read what under the old scheme.
CREATE TABLE notification_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    notification_id INTEGER NOT NULL,
    staff_id INTEGER NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    is_cleared INTEGER NOT NULL DEFAULT 0,
    read_at TIMESTAMP,
    cleared_at TIMESTAMP,
    UNIQUE (notification_id, staff_id),
    -- ON DELETE CASCADE: routes/requests.py's approve/deny/cancel flow
    -- (via clear_unrestriction_pending_notifications) hard-deletes the
    -- 'unrestriction_requested' notification row once it's resolved.
    -- Without cascading, that DELETE would fail outright (FOREIGN KEY
    -- constraint failed) the moment any admin/in_charge had already
    -- marked their copy read -- connections in this app run with
    -- PRAGMA foreign_keys = ON (see database.py), so this isn't
    -- optional.
    FOREIGN KEY (notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
    FOREIGN KEY (staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_notification_reads_staff_id ON notification_reads(staff_id);
CREATE INDEX idx_notification_reads_notification_id ON notification_reads(notification_id);
