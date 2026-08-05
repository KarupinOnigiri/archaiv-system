-- Follow-up to 0014_add_checkout_session_last_activity.sql.
--
-- That migration added the last_activity_at column and backfilled rows
-- that existed *at the time it ran*. But create_checkout_session() was
-- not yet updated to set last_activity_at on new INSERTs, so any
-- session started between running that migration and deploying the
-- fixed transactions.py still has last_activity_at = NULL (e.g. session
-- 15, which is what triggered the TypeError in checkout_resume_form).
--
-- Safe to run any number of times -- only touches rows that are still
-- NULL.
UPDATE checkout_sessions
SET last_activity_at = created_at
WHERE last_activity_at IS NULL;
