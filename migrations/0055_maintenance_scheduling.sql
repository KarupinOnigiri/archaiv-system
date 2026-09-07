-- 0055_maintenance_scheduling.sql
-- Proactive maintenance scheduling, on top of the existing reactive
-- "mark for maintenance / resolve maintenance" flow (assets.status =
-- 'maintenance', services/asset_service.py's resolve_maintenance).
--
-- next_maintenance_due: an explicit calendar date an admin/in_charge sets
-- (e.g. "inspect annually"). maintenance_interval_checkouts +
-- checkouts_since_maintenance: an alternative usage-based trigger (e.g.
-- "service this after every 20 checkouts") - checkouts_since_maintenance
-- increments on every commit-checkout of a serialized asset (see
-- services/transaction_service.py's finish_checkout_session) and resets to
-- 0 whenever resolve_maintenance runs. Both are optional and independent -
-- an asset can use one, the other, both, or neither.
ALTER TABLE assets ADD COLUMN next_maintenance_due DATE;
ALTER TABLE assets ADD COLUMN maintenance_interval_checkouts INTEGER;
ALTER TABLE assets ADD COLUMN checkouts_since_maintenance INTEGER NOT NULL DEFAULT 0;
CREATE INDEX idx_assets_next_maintenance_due ON assets(next_maintenance_due);

-- How many days ahead of next_maintenance_due (or how close to
-- maintenance_interval_checkouts) counts as "due soon" for the dashboard
-- card / notification sweep - admin-configurable, mirrors
-- checkout_session_stale_minutes' pattern (migration 0050).
INSERT INTO app_settings (key, value) VALUES ('maintenance_reminder_lead_days', '7');
