-- 0023_feedback_reports.sql
-- Backs the "Give feedback" row in the account dropdown (templates/base.html)
-- -- previously just a toast, now a real in-app bug/issue report. One row
-- per submission; reviewed by admins from a new card on the System page
-- (routes/feedback.py). No recipient targeting needed the way notifications
-- has (0021) -- feedback is always admin-only, so a plain role check in
-- the route is enough, no per-row recipient columns.
CREATE TABLE feedback_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL,
    category TEXT NOT NULL CHECK (category IN ('bug', 'idea', 'other')),
    message TEXT NOT NULL,
    page_path TEXT,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    resolved_at TIMESTAMP,
    resolved_by_staff_id INTEGER,
    FOREIGN KEY (staff_id) REFERENCES staff(id),
    FOREIGN KEY (resolved_by_staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_feedback_reports_status ON feedback_reports(status);
CREATE INDEX idx_feedback_reports_staff_id ON feedback_reports(staff_id);
