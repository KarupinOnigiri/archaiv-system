-- 0047_add_pi_performance_indexes.sql
-- Indexes for columns that are filtered/joined on every request but were
-- never covered -- unlike columns with a UNIQUE constraint (sessions.token,
-- staff.username, assets.barcode_id, ...), which SQLite already auto-indexes.
--
-- audit_log is the one that matters most: it grows forever (every action
-- in the app logs a row) and app.py's inject_csrf_token + routes/staff.py's
-- get_audit_log_context both filter/sort it on every Activity Log view and
-- non-admin page load. Without an index this becomes a full table scan
-- that gets slower every week the app stays deployed -- exactly the kind
-- of slow-degradation that's easy to miss in dev (small table) and painful
-- on a Pi (slow storage) months into a real deployment.
CREATE INDEX IF NOT EXISTS idx_audit_log_staff_id ON audit_log(staff_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log(target_table, target_id);

-- sessions.staff_id -- used by every "log this account out everywhere"
-- action (password reset, lock, deactivate, ...) and cleanup_expired_
-- sessions' periodic sweep (app.py's scheduler, every 30 min).
CREATE INDEX IF NOT EXISTS idx_sessions_staff_id ON sessions(staff_id);

-- assets.deleted_at / status -- every Inventory table/stat-card query
-- filters on these (WHERE deleted_at IS NULL, WHERE status = ...).
CREATE INDEX IF NOT EXISTS idx_assets_deleted_at ON assets(deleted_at);
CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_category_id ON assets(category_id);

-- members.department_id / is_active -- same story for the Members page
-- and every department-scoped report/filter.
CREATE INDEX IF NOT EXISTS idx_members_department_id ON members(department_id);
CREATE INDEX IF NOT EXISTS idx_members_is_active ON members(is_active);

-- staff.role / is_active -- filtered together on nearly every Staff page
-- load (active-by-role stat cards) and in seed_service.seed_initial_admin,
-- which runs on every app startup.
CREATE INDEX IF NOT EXISTS idx_staff_role_is_active ON staff(role, is_active);
