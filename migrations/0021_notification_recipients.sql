-- 0021_notification_recipients.sql
-- Generalizes notifications (see 0016_notifications.sql) beyond loan
-- due/overdue alerts to support targeted recipients -- specifically, the
-- unrestriction-request lifecycle: admin/in_charge get notified when a
-- tool_keeper files one, and the original requester gets notified once
-- it's approved or denied (see services/notification_service.py's
-- create_unrestriction_requested_notifications /
-- create_unrestriction_resolved_notification, called from routes/requests.py).
--
-- recipient_staff_id targets one specific person (the requester, for the
-- resolved notice). recipient_role broadcasts to everyone with that role
-- (admin/in_charge, for the pending notice) -- the existing global-shared-
-- read-state design 0016 describes still applies *within* a broadcast
-- group (any admin marking it read doesn't affect in_charge's copy, since
-- each role gets its own row), it's just no longer *always* everyone.
-- Existing due_soon/overdue rows get both columns NULL on migration,
-- which the app continues to treat as "visible to everyone" -- unchanged
-- behavior for the loan-alert half of this table.
--
-- SQLite can't ALTER a CHECK constraint or a column's nullability
-- directly, so the table is rebuilt (same pattern as
-- 0007_unrestriction_cancel.sql), preserving every existing row.

PRAGMA foreign_keys = OFF;

CREATE TABLE notifications_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('due_soon', 'overdue', 'unrestriction_requested', 'unrestriction_resolved')),
    transaction_id INTEGER,
    unrestriction_request_id INTEGER,
    recipient_staff_id INTEGER,
    recipient_role TEXT CHECK (recipient_role IN ('admin', 'in_charge', 'tool_keeper')),
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    -- No UNIQUE on (unrestriction_request_id, type, recipient_*) -- every
    -- legitimate row here always has exactly one of recipient_staff_id/
    -- recipient_role NULL (they're mutually exclusive by design: one
    -- targets a person, the other a role), and SQLite treats NULL as
    -- distinct from NULL in a UNIQUE constraint, so a tuple UNIQUE
    -- including either column could never actually fire -- verified this
    -- empirically rather than assume it. Duplicate notifications are
    -- prevented at the application level instead: routes/requests.py's
    -- create/approve/deny/cancel handlers already refuse to act twice on
    -- the same request (the existing-pending-request 409, and the
    -- already-resolved 409), so a second identical insert can't happen
    -- through normal use.
    UNIQUE (transaction_id, type),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (unrestriction_request_id) REFERENCES unrestriction_requests(id),
    FOREIGN KEY (recipient_staff_id) REFERENCES staff(id)
);

INSERT INTO notifications_new (id, type, transaction_id, message, is_read, created_at)
SELECT id, type, transaction_id, message, is_read, created_at FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_is_read ON notifications(is_read);
CREATE INDEX idx_notifications_transaction_id ON notifications(transaction_id);
CREATE INDEX idx_notifications_unrestriction_request_id ON notifications(unrestriction_request_id);
CREATE INDEX idx_notifications_recipient_staff_id ON notifications(recipient_staff_id);
CREATE INDEX idx_notifications_recipient_role ON notifications(recipient_role);

PRAGMA foreign_keys = ON;
