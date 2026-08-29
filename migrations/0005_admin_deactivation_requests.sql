-- 0005_admin_deactivation_requests.sql
-- Lets an admin request another admin's deactivation instead of doing it
-- unilaterally. It only takes effect once enough approvals come in - the
-- required tier (admin / in_charge / tool_keeper) and count are decided
-- once at request-creation time (see services/admin_deactivation_service.py)
-- and snapshotted onto the request row, so staff counts changing later
-- can't shift the bar underneath an in-progress request.
--
-- Denial works differently from approval: a single denial from anyone
-- eligible for the request's tier cancels it outright (no consensus
-- needed to reject), and the requester can cancel their own pending
-- request the same way.

CREATE TABLE "admin_deactivation_requests" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_staff_id INTEGER NOT NULL,
    requested_by_staff_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'cancelled')),
    required_tier TEXT NOT NULL CHECK (required_tier IN ('admin', 'in_charge', 'tool_keeper')),
    required_approvals INTEGER NOT NULL,
    denied_by_staff_id INTEGER,
    denial_reason TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    resolved_at TIMESTAMP,
    FOREIGN KEY (target_staff_id) REFERENCES staff(id),
    FOREIGN KEY (requested_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (denied_by_staff_id) REFERENCES staff(id)
);

CREATE TABLE "admin_deactivation_approvals" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    approver_staff_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (request_id, approver_staff_id),
    FOREIGN KEY (request_id) REFERENCES admin_deactivation_requests(id),
    FOREIGN KEY (approver_staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_admin_deactivation_requests_status ON admin_deactivation_requests(status);
CREATE INDEX idx_admin_deactivation_requests_target ON admin_deactivation_requests(target_staff_id);
CREATE INDEX idx_admin_deactivation_approvals_request ON admin_deactivation_approvals(request_id);