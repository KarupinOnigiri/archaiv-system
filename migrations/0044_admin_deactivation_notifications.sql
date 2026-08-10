-- 0044_admin_deactivation_notifications.sql
-- Wires admin-deactivation requests into the shared notifications table
-- (0016/0021/0027/0035), the same way unrestriction requests already are:
-- 'admin_deactivation_requested' rows tell every eligible approver a
-- request needs their attention (recipient_role, one row per eligible
-- role -- see services/notification_service.py's
-- create_admin_deactivation_requested_notifications, called from
-- routes/staff.py's request_admin_deactivation). Previously this event
-- only wrote to the Activity Log and bumped the Approvals sidebar badge
-- count -- nothing ever landed in the bell, so eligible approvers had no
-- way to learn a request existed short of happening to click into
-- Approvals themselves.
--
-- admin_deactivation_request_id mirrors unrestriction_request_id (0021) --
-- lets the bell's "open" deep-link (routes/notifications.py) jump
-- straight to the Approvals > Admin Deactivation tab.
--
-- SQLite can't ALTER a CHECK constraint in place, so the table is rebuilt
-- (same pattern as 0021/0035), preserving every existing row.

PRAGMA foreign_keys = OFF;

CREATE TABLE notifications_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN (
        'due_soon', 'overdue', 'unrestriction_requested', 'unrestriction_resolved',
        'feedback_submitted', 'feedback_resolved',
        'admin_deactivation_requested', 'admin_deactivation_resolved'
    )),
    transaction_id INTEGER,
    unrestriction_request_id INTEGER,
    feedback_report_id INTEGER,
    admin_deactivation_request_id INTEGER,
    recipient_staff_id INTEGER,
    recipient_role TEXT CHECK (recipient_role IN ('admin', 'in_charge', 'tool_keeper')),
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (transaction_id, type),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (unrestriction_request_id) REFERENCES unrestriction_requests(id),
    FOREIGN KEY (feedback_report_id) REFERENCES feedback_reports(id),
    FOREIGN KEY (admin_deactivation_request_id) REFERENCES admin_deactivation_requests(id),
    FOREIGN KEY (recipient_staff_id) REFERENCES staff(id)
);

INSERT INTO notifications_new (
    id, type, transaction_id, unrestriction_request_id, feedback_report_id,
    recipient_staff_id, recipient_role, message, is_read, created_at
)
SELECT
    id, type, transaction_id, unrestriction_request_id, feedback_report_id,
    recipient_staff_id, recipient_role, message, is_read, created_at
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_is_read ON notifications(is_read);
CREATE INDEX idx_notifications_transaction_id ON notifications(transaction_id);
CREATE INDEX idx_notifications_unrestriction_request_id ON notifications(unrestriction_request_id);
CREATE INDEX idx_notifications_feedback_report_id ON notifications(feedback_report_id);
CREATE INDEX idx_notifications_admin_deactivation_request_id ON notifications(admin_deactivation_request_id);
CREATE INDEX idx_notifications_recipient_staff_id ON notifications(recipient_staff_id);
CREATE INDEX idx_notifications_recipient_role ON notifications(recipient_role);

PRAGMA foreign_keys = ON;
