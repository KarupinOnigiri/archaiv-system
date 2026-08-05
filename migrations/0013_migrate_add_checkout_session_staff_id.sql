-- Adds staff ownership to checkout_sessions, so an in-progress checkout
-- can be scoped to (and only resumable by) the staff member who started
-- it, rather than being visible to any logged-in staff.
--
-- Existing rows get NULL staff_id (unknown owner). In application code,
-- a NULL staff_id is treated as "not owned by anyone" -- it won't show
-- up in any staff's active-checkout dock/banner, but it also won't
-- block a member from starting a fresh session (the "member already has
-- an open session" check is unaffected, since that's keyed on member_id
-- and status, not staff_id). If you have any sessions with status='open'
-- at migration time, either let them go stale on their own
-- (SESSION_STALE_MINUTES) or manually assign/cancel them.
--
-- Run this once against your SQLite DB, e.g.:
--   sqlite3 your_database.db < migrate_add_checkout_session_staff_id.sql
-- Adjust if you're using Flask-Migrate/Alembic -- wrap this in a proper
-- migration script instead of running it directly, so it's tracked.

ALTER TABLE checkout_sessions ADD COLUMN staff_id INTEGER REFERENCES staff(id);
