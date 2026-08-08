-- 0036_admin_promotion_requests.sql
-- Peer-approved "hand over admin" request -- an admin nominates a
-- non-admin staff member to be promoted to admin, and it only takes
-- effect once enough approvals come in, the same quorum shape as
-- admin_deactivation_requests (0005) rather than a unilateral role
-- change (routes/staff.py's edit_staff_submit already blocks moving an
-- existing admin OUT of the role directly; this table is the equally
-- guarded path for moving someone INTO it). See
-- services/admin_deactivation_service.py's determine_approval_tier
-- (reused as-is -- it's generic staff-count logic, not deactivation-
-- specific) and finalize_promotion_if_ready.
--
-- Deliberately a separate table from admin_deactivation_requests rather
-- than a shared/polymorphic one -- promotion and deactivation have
-- different finalization effects (role='admin' vs is_active=0) and
-- different floor checks (deactivation refuses to zero out all admins;
-- promotion has no such floor), so keeping them structurally separate
-- avoids a shared table with columns that are meaningless for one type
-- or the other.

CREATE TABLE "admin_promotion_requests" (
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

CREATE TABLE "admin_promotion_approvals" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    approver_staff_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (request_id, approver_staff_id),
    FOREIGN KEY (request_id) REFERENCES admin_promotion_requests(id),
    FOREIGN KEY (approver_staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_admin_promotion_requests_status ON admin_promotion_requests(status);
CREATE INDEX idx_admin_promotion_requests_target ON admin_promotion_requests(target_staff_id);
CREATE INDEX idx_admin_promotion_approvals_request ON admin_promotion_approvals(request_id);
