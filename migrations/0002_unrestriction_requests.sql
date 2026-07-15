-- 0002_unrestriction_requests.sql
-- Lets tool_keeper flag a restricted member for review instead of lifting
-- the restriction directly (that stays admin/in_charge only). admin or
-- in_charge then approves (which unrestricts the member) or denies it.

CREATE TABLE "unrestriction_requests" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL,
    requested_by_staff_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied')),
    reviewed_by_staff_id INTEGER,
    review_notes TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    reviewed_at TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES members(id),
    FOREIGN KEY (requested_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (reviewed_by_staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_unrestriction_requests_status ON unrestriction_requests(status);
CREATE INDEX idx_unrestriction_requests_member_id ON unrestriction_requests(member_id);