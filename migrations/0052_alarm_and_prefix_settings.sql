-- 0052_alarm_and_prefix_settings.sql
-- Two new admin-editable app_settings rows:
-- - alarm_enabled: lets an admin mute the RFID exit-monitoring alarm itself
--   (the banner/active-alarm state) without disabling the whole Monitoring
--   feature - detections still get classified and logged either way. See
--   services/monitoring_service.py's record_rfid_event.
-- - asset_id_prefix / member_code_prefix / staff_code_prefix: the "AST"/
--   "MBR"/"STF" prefixes every generated asset_id/member_code/staff_code
--   used to be hardcoded to (services/asset_service.py, member_service.py,
--   staff_service.py, setup_service.py) - seeded here with those same
--   values so nothing changes until an admin actually edits one. Changing a
--   prefix only affects codes generated from that point on - existing
--   records keep whatever prefix they already have (see settings_service.py's
--   getters/System page's new cards for where these are edited).

INSERT INTO app_settings (key, value) VALUES
    ('alarm_enabled', '1'),
    ('asset_id_prefix', 'AST'),
    ('member_code_prefix', 'MBR'),
    ('staff_code_prefix', 'STF');
