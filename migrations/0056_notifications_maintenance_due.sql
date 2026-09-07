-- 0056_notifications_maintenance_due.sql
-- Adds a 'maintenance_due' notification type + an asset_id FK, so
-- services/maintenance_service.py's sweep_maintenance_due() can notify
-- staff the same way sync_loan_notifications() already does for
-- due_soon/overdue - see that function's INSERT OR IGNORE + UNIQUE
-- constraint pattern, mirrored here with UNIQUE(asset_id, type) alongside
-- the existing UNIQUE(transaction_id, type).
--
-- SQLite can't ALTER a CHECK constraint in place, so the table is rebuilt
-- (same pattern as 0021/0035/0044), preserving every existing row.

PRAGMA foreign_keys = OFF;

CREATE TABLE notifications_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN (
        'due_soon', 'overdue', 'unrestriction_requested', 'unrestriction_resolved',
        'feedback_submitted', 'feedback_resolved',
        'admin_deactivation_requested', 'admin_deactivation_resolved',
        'maintenance_due'
    )),
    transaction_id INTEGER,
    unrestriction_request_id INTEGER,
    feedback_report_id INTEGER,
    admin_deactivation_request_id INTEGER,
    asset_id INTEGER,
    recipient_staff_id INTEGER,
    recipient_role TEXT CHECK (recipient_role IN ('admin', 'in_charge', 'tool_keeper')),
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (transaction_id, type),
    UNIQUE (asset_id, type),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id),
    FOREIGN KEY (unrestriction_request_id) REFERENCES unrestriction_requests(id),
    FOREIGN KEY (feedback_report_id) REFERENCES feedback_reports(id),
    FOREIGN KEY (admin_deactivation_request_id) REFERENCES admin_deactivation_requests(id),
    FOREIGN KEY (asset_id) REFERENCES assets(id),
    FOREIGN KEY (recipient_staff_id) REFERENCES staff(id)
);

INSERT INTO notifications_new (
    id, type, transaction_id, unrestriction_request_id, feedback_report_id,
    admin_deactivation_request_id, recipient_staff_id, recipient_role, message, is_read, created_at
)
SELECT
    id, type, transaction_id, unrestriction_request_id, feedback_report_id,
    admin_deactivation_request_id, recipient_staff_id, recipient_role, message, is_read, created_at
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

CREATE INDEX idx_notifications_is_read ON notifications(is_read);
CREATE INDEX idx_notifications_transaction_id ON notifications(transaction_id);
CREATE INDEX idx_notifications_unrestriction_request_id ON notifications(unrestriction_request_id);
CREATE INDEX idx_notifications_feedback_report_id ON notifications(feedback_report_id);
CREATE INDEX idx_notifications_admin_deactivation_request_id ON notifications(admin_deactivation_request_id);
CREATE INDEX idx_notifications_asset_id ON notifications(asset_id);
CREATE INDEX idx_notifications_recipient_staff_id ON notifications(recipient_staff_id);
CREATE INDEX idx_notifications_recipient_role ON notifications(recipient_role);

PRAGMA foreign_keys = ON;
