-- 0035_add_feedback_notification_types.sql
-- Wires feedback into the shared notifications table (0016/0021/0027)
-- instead of a parallel channel: 'feedback_submitted' rows tell admins a
-- new report came in (recipient_role='admin') and confirm to the submitter
-- their report was sent (recipient_staff_id=submitter); 'feedback_resolved'
-- rows carry the admin's reply back to the submitter once resolved. See
-- services/notification_service.py's create_feedback_submitted_notifications
-- / create_feedback_resolved_notification, called from routes/feedback.py.
--
-- feedback_report_id mirrors how unrestriction_request_id was added in
-- 0021 -- lets the bell's "open" deep-link (routes/notifications.py) jump
-- straight to the relevant report the same way it already does for
-- unrestriction requests.
--
-- SQLite can't ALTER a CHECK constraint in place, so the table is rebuilt
-- (same pattern as 0021_notification_recipients.sql), preserving every
-- existing row.

PRAGMA foreign_keys = OFF;

CREATE TABLE notifications_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN (
        'due_soon', 'overdue', 'unrestriction_requested', 'unrestriction_resolved',
        'feedback_submitted', 'feedback_resolved'
    )),
    transaction_id INTEGER,
    unrestriction_request_id INTEGER,
    feedback_report_id INTEGER,
    recipient_staff_id INTEGER,
    recipient_role TEXT CHECK (recipient_role IN ('admin', 'in_charge', 'tool_keeper')),
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (transaction_id, type),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (unrestriction_request_id) REFERENCES unrestriction_requests(id),
    FOREIGN KEY (feedback_report_id) REFERENCES feedback_reports(id),
    FOREIGN KEY (recipient_staff_id) REFERENCES staff(id)
);

INSERT INTO notifications_new (
    id, type, transaction_id, unrestriction_request_id, recipient_staff_id,
    recipient_role, message, is_read, created_at
)
SELECT
    id, type, transaction_id, unrestriction_request_id, recipient_staff_id,
    recipient_role, message, is_read, created_at
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_is_read ON notifications(is_read);
CREATE INDEX idx_notifications_transaction_id ON notifications(transaction_id);
CREATE INDEX idx_notifications_unrestriction_request_id ON notifications(unrestriction_request_id);
CREATE INDEX idx_notifications_feedback_report_id ON notifications(feedback_report_id);
CREATE INDEX idx_notifications_recipient_staff_id ON notifications(recipient_staff_id);
CREATE INDEX idx_notifications_recipient_role ON notifications(recipient_role);

PRAGMA foreign_keys = ON;
