-- 0049_rfid_events_borrower.sql
-- Adds the borrower to an 'authorized' detection event, captured at record
-- time (services/monitoring_service.py's record_rfid_event) rather than
-- looked up live when the Monitoring table is viewed later -- the
-- transaction backing that loan can be returned (status flips to
-- 'returned') well before anyone looks at the log, at which point a live
-- "who currently has this asset borrowed" query would come back empty.
-- Recording it at detection time keeps the log an accurate point-in-time
-- record regardless of what happens to the loan afterward.
--
-- Nullable and ON DELETE SET NULL, same reasoning as asset_id on this table:
-- a later member deletion shouldn't be blocked by, or corrupt, historical
-- detection events. Always NULL for 'unauthorized'/'unknown_tag' events --
-- there is no borrower to attribute those to.
ALTER TABLE rfid_events ADD COLUMN member_id INTEGER REFERENCES members(id) ON DELETE SET NULL;

CREATE INDEX idx_rfid_events_member_id ON rfid_events(member_id);
