-- 0048_rfid_monitoring.sql
-- Backs the new Monitoring tab: one row per tagged-tool detection event
-- reported by the fixed UHF RFID Reader Antenna at the monitored exit.
--
-- classification is derived at insert time (services/monitoring_service.py's
-- record_rfid_event) from the matched asset's current status, mirroring the
-- thesis's rule: a tool with an active authorized borrowing record is
-- treated as authorized for exit monitoring, everything else is not.
-- 'unknown_tag' is its own outcome (not folded into 'unauthorized') because
-- a stray/mis-tagged item is a different failure mode than a real tool
-- leaving without authorization, and the two shouldn't be conflated in the
-- confusion-matrix data this table is meant to support.
--
-- asset_id is nullable and ON DELETE SET NULL (not a hard requirement) so a
-- later asset deletion can't be blocked by, or silently corrupt, historical
-- detection events -- the raw rfid_tag_scanned is kept either way.
--
-- alarm_deactivated_at/deactivated_by stay NULL until the Admin's physical
-- Master Key Card tap at the HF/NFC reader clears the alarm (see
-- services/monitoring_service.py's deactivate_active_alarms) -- there is
-- deliberately no software-only way to clear an active alarm, matching the
-- study's requirement that deactivation requires physical presence at the
-- site of the alert.
CREATE TABLE "rfid_events" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rfid_tag_scanned TEXT NOT NULL,
    asset_id INTEGER,
    classification TEXT NOT NULL CHECK (classification IN ('authorized', 'unauthorized', 'unknown_tag')),
    alarm_activated INTEGER NOT NULL DEFAULT 0,
    detected_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    alarm_deactivated_at TIMESTAMP,
    deactivated_by INTEGER,
    notes TEXT,
    FOREIGN KEY (asset_id) REFERENCES assets(id) ON DELETE SET NULL,
    FOREIGN KEY (deactivated_by) REFERENCES staff(id) ON DELETE SET NULL
);

CREATE INDEX idx_rfid_events_detected_at ON rfid_events(detected_at);
CREATE INDEX idx_rfid_events_classification ON rfid_events(classification);
CREATE INDEX idx_rfid_events_asset_id ON rfid_events(asset_id);
-- Speeds up "is an alarm currently active?" (WHERE alarm_activated = 1 AND
-- alarm_deactivated_at IS NULL), which the alarm banner checks on every poll.
CREATE INDEX idx_rfid_events_active_alarm ON rfid_events(alarm_activated, alarm_deactivated_at);

-- New tab is off by default until the Pi's RFID listener is actually wired
-- up and reporting events -- an admin flips it on from System once hardware
-- is deployed, same as any other feature flag here.
INSERT INTO app_settings (key, value) VALUES ('feature_monitoring_enabled', '0');
