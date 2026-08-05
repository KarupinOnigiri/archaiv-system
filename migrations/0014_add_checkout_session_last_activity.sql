-- Adds last_activity_at to checkout_sessions so staleness is measured
-- from actual activity (as _get_active_checkout_session's docstring
-- already promises) rather than from when the session was first opened.
--
-- SQLite's ALTER TABLE ... ADD COLUMN only accepts a *constant* default
-- (e.g. NULL, a literal string/number) -- not a function call like
-- datetime('now', 'localtime'). That expression is only allowed as a
-- default in CREATE TABLE. So the column is added as NULL here, then
-- backfilled explicitly in the UPDATE below.
ALTER TABLE checkout_sessions
    ADD COLUMN last_activity_at TIMESTAMP;

-- Backfill every existing row (open, completed, or cancelled) so
-- last_activity_at is never NULL going forward. Using created_at as the
-- starting point for pre-existing sessions is the closest available
-- approximation of "last known activity" for rows that predate this
-- column.
UPDATE checkout_sessions
SET last_activity_at = created_at
WHERE last_activity_at IS NULL;
