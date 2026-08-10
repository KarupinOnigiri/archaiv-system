-- 0045_admin_deactivation_review_workflow.sql
-- Reworks admin-deactivation approval from "hit a role-gated single tier,
-- auto-resolves" into a peer-review-then-requester-decides model:
--
--   - Every active in_charge/tool_keeper account can weigh in on any
--     pending request (no more role == required_tier gating) -- approving
--     AND denying are both just individual votes now, recorded here, never
--     auto-resolving the request by themselves.
--   - required_tier still records which composition tier applied at
--     request time, but its meaning changes from "the one role allowed to
--     approve" to "how big a consensus is expected" -- 'full' (4 total,
--     >=2 in_charge), 'reduced' (3 total, >=1 in_charge), 'minimal' (2
--     total, >=1 in_charge), picked by how many in_charge/tool_keeper
--     accounts actually exist (see determine_approval_tier). No more
--     'admin' tier -- other admins no longer cast approve/deny votes here.
--   - required_in_charge is new: the minimum number of those approvals
--     that must specifically come from in_charge accounts.
--   - admin_deactivation_approvals gets a decision + reason so a deny is
--     just as much a recorded vote as an approve, instead of instantly
--     flipping the request to 'denied'.
--   - Reaching the required counts no longer deactivates anything by
--     itself -- only the requester finalizing it does (see
--     finalize_admin_deactivation / the new /finalize route), after
--     reviewing every vote and its reason. The requester can still cancel
--     at any point regardless of vote counts.
--
-- SQLite can't ALTER a CHECK constraint or add one to an existing column
-- in place, so both tables are rebuilt, preserving every existing row.

PRAGMA foreign_keys = OFF;

CREATE TABLE admin_deactivation_requests_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_staff_id INTEGER NOT NULL,
    requested_by_staff_id INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'denied', 'cancelled')),
    required_tier TEXT NOT NULL CHECK (required_tier IN ('full', 'reduced', 'minimal')),
    required_approvals INTEGER NOT NULL,
    required_in_charge INTEGER NOT NULL DEFAULT 1,
    denied_by_staff_id INTEGER,
    denial_reason TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    resolved_at TIMESTAMP,
    FOREIGN KEY (target_staff_id) REFERENCES staff(id),
    FOREIGN KEY (requested_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (denied_by_staff_id) REFERENCES staff(id)
);

-- Old tiers map onto the closest new composition tier. required_in_charge
-- has no real historical value to recover, so it's backfilled to 1 (the
-- floor every new tier already requires) -- only matters going forward
-- for requests still pending at migration time, which finalize_admin_
-- deactivation will re-evaluate against the live in_charge/tool_keeper
-- counts as normal.
INSERT INTO admin_deactivation_requests_new
SELECT id, target_staff_id, requested_by_staff_id, reason, status,
       CASE required_tier
           WHEN 'admin' THEN 'full'
           WHEN 'in_charge' THEN 'reduced'
           ELSE 'minimal'
       END,
       required_approvals, 1, denied_by_staff_id, denial_reason, created_at, resolved_at
FROM admin_deactivation_requests;

DROP TABLE admin_deactivation_requests;
ALTER TABLE admin_deactivation_requests_new RENAME TO admin_deactivation_requests;

CREATE TABLE admin_deactivation_approvals_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    approver_staff_id INTEGER NOT NULL,
    decision TEXT NOT NULL DEFAULT 'approve' CHECK (decision IN ('approve', 'deny')),
    reason TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (request_id, approver_staff_id),
    FOREIGN KEY (request_id) REFERENCES admin_deactivation_requests(id),
    FOREIGN KEY (approver_staff_id) REFERENCES staff(id)
);

-- Every existing row here was an approval (denials used to live directly
-- on the request row, not as their own rows), so they all migrate as
-- decision='approve'.
INSERT INTO admin_deactivation_approvals_new (id, request_id, approver_staff_id, decision, created_at)
SELECT id, request_id, approver_staff_id, 'approve', created_at
FROM admin_deactivation_approvals;

DROP TABLE admin_deactivation_approvals;
ALTER TABLE admin_deactivation_approvals_new RENAME TO admin_deactivation_approvals;

CREATE INDEX idx_admin_deactivation_requests_status ON admin_deactivation_requests(status);
CREATE INDEX idx_admin_deactivation_requests_target ON admin_deactivation_requests(target_staff_id);
CREATE INDEX idx_admin_deactivation_approvals_request ON admin_deactivation_approvals(request_id);

PRAGMA foreign_keys = ON;
