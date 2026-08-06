-- 0016_notifications.sql
-- Backs the in-app Notifications bell/tab with real due-soon/overdue
-- alerts instead of the static placeholder markup that used to live in
-- base.html. One row per (transaction, type) pair -- the UNIQUE
-- constraint lets the sync job INSERT OR IGNORE without duplicating an
-- alert every time it runs on its schedule. Notifications are global
-- (shared read state across all staff), not per-staff-member -- this is
-- a small shop with a handful of staff on one shift, not a multi-tenant
-- inbox, so "someone already saw this" is meaningful information for
-- everyone else too.

CREATE TABLE "notifications" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL CHECK (type IN ('due_soon', 'overdue')),
    transaction_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (transaction_id, type),
    FOREIGN KEY (transaction_id) REFERENCES transactions(id)
);

CREATE INDEX idx_notifications_is_read ON notifications(is_read);
CREATE INDEX idx_notifications_transaction_id ON notifications(transaction_id);
