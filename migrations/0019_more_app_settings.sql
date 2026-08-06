-- 0019_more_app_settings.sql
-- Extends app_settings (0018) with three more previously-hardcoded
-- constants that now live on the new System page: account lockout
-- (routes/auth.py's old MAX_FAILED_ATTEMPTS/LOCKOUT_MINUTES) and the
-- notification due-soon threshold (services/notification_service.py's
-- old DUE_SOON_HOURS). Seeded with those same old constants' values.

INSERT INTO app_settings (key, value) VALUES
    ('max_failed_attempts', '5'),
    ('lockout_minutes', '15'),
    ('due_soon_hours', '24');
