-- 0007_unrestriction_cancel.sql
-- Adds 'cancelled' to unrestriction_requests.status so the original
-- requester can withdraw their own pending request, mirroring the
-- requester-self-cancel behavior admin_deactivation_requests already has.
-- SQLite can't ALTER a CHECK constraint directly, so the table is rebuilt
-- (same pattern as 0006_departments_categories.sql) with every other
-- column, constraint, and row preserved as-is.

PRAGMA foreign_keys = OFF;

CREATE TABLE unrestriction_requests_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL,
    requested_by_staff_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'cancelled')),
    reviewed_by_staff_id INTEGER,
    review_notes TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    reviewed_at TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id),
    FOREIGN KEY (requested_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (reviewed_by_staff_id) REFERENCES staff(id)
);

INSERT INTO unrestriction_requests_new
SELECT id, member_id, requested_by_staff_id, reason, status,
       reviewed_by_staff_id, review_notes, created_at, reviewed_at
FROM unrestriction_requests;

DROP TABLE unrestriction_requests;
ALTER TABLE unrestriction_requests_new RENAME TO unrestriction_requests;

CREATE INDEX idx_unrestriction_requests_status ON unrestriction_requests(status);
CREATE INDEX idx_unrestriction_requests_member_id ON unrestriction_requests(member_id);

PRAGMA foreign_keys = ON;
