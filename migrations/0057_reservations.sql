-- 0057_reservations.sql
-- Advance/future-dated booking of a specific asset for a member - distinct
-- from assets.status='reserved', which means exclusively "sitting in an
-- open checkout-session cart right now" (claimed via the atomic CAS in
-- services/transaction_service.py's add_item_to_checkout_session). Advance
-- bookings live entirely in this new table and never touch assets.status -
-- see services/reservation_service.py's module docstring for the full
-- reasoning and the v1 scope decision (advisory, not enforced: a reserved
-- asset can still be checked out today by anyone).
--
-- Mirrors unrestriction_requests' shape (migration 0002) - a status-flow
-- entity tied to an asset/member/staff, reviewed/resolved over time.
CREATE TABLE "reservations" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    staff_id INTEGER NOT NULL,
    start_at TIMESTAMP NOT NULL,
    end_at TIMESTAMP NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled'
        CHECK (status IN ('scheduled', 'fulfilled', 'cancelled', 'expired')),
    notes TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    cancelled_at TIMESTAMP,
    cancelled_by_staff_id INTEGER,
    fulfilled_transaction_id INTEGER,
    FOREIGN KEY (asset_id) REFERENCES assets(id),
    FOREIGN KEY (member_id) REFERENCES members(id),
    FOREIGN KEY (staff_id) REFERENCES staff(id),
    FOREIGN KEY (cancelled_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (fulfilled_transaction_id) REFERENCES transactions(id),
    CHECK (end_at > start_at)
);
CREATE INDEX idx_reservations_asset_id ON reservations(asset_id);
CREATE INDEX idx_reservations_member_id ON reservations(member_id);
CREATE INDEX idx_reservations_status ON reservations(status);
CREATE INDEX idx_reservations_start_at ON reservations(start_at);

-- Off by default - an admin opts in via System settings, same rollout
-- convention as monitoring (migration 0048).
INSERT INTO app_settings (key, value) VALUES ('feature_reservations_enabled', '0');
