-- 0033_feature_flags.sql
-- Lets an admin turn whole tabs/workflows off from the System page
-- (routes/staff.py's feature-flags endpoints, services/settings_service.py's
-- get_feature_flags) without touching code. Reuses the generic app_settings
-- key/value store from 0018 rather than a dedicated table -- these are
-- plain booleans stored the same "stringified value" way the numeric
-- settings already are, just parsed with get_setting_bool instead of
-- get_setting_int.
--
-- Notifications/Feedback/Staff/Activity Log/System themselves are
-- deliberately NOT toggleable here -- they're core admin infrastructure,
-- not optional workflows, and Feedback in particular needs to stay on so
-- staff can still report a problem with a disabled feature.
--
-- Seeded '1' (enabled) so nothing changes behaviorally until an admin
-- actually disables something.
INSERT INTO app_settings (key, value) VALUES
    ('feature_assets_enabled', '1'),
    ('feature_members_enabled', '1'),
    ('feature_transactions_enabled', '1'),
    ('feature_requests_enabled', '1'),
    ('feature_reports_enabled', '1');
