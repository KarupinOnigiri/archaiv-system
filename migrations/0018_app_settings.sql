-- 0018_app_settings.sql
-- Generic key/value store for admin-editable global settings that used
-- to be hardcoded Python constants -- starting with the two session
-- timeout values (services/auth_service.py's SESSION_IDLE_MINUTES and
-- routes/auth.py's SESSION_ABSOLUTE_HOURS). Seeded with those same old
-- constants' values so nothing changes behaviorally until an admin
-- actually edits a setting. A generic table (rather than dedicated
-- columns/rows like loan_limits) since these settings don't share a
-- natural "type" axis the way per-member-type loan caps do.

CREATE TABLE "app_settings" (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO app_settings (key, value) VALUES
    ('session_idle_minutes', '30'),
    ('session_absolute_hours', '12');
