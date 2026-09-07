-- 0054_asset_type_and_consumables.sql
-- Adds a second kind of asset row: a quantity-based consumable stock item
-- (e.g. "50x safety goggles") alongside the existing serialized ("one row =
-- one physical tool with its own barcode/RFID") assets. asset_type has no
-- inline CHECK - no existing migration in this repo does that on ADD COLUMN,
-- so validation stays in services/asset_service.py, same as every other
-- enum-shaped field validated in the service layer rather than the schema
-- (see e.g. VALID_CONDITIONS).
--
-- quantity/reorder_threshold are NULL for serialized assets (nothing to
-- track) and expected NOT NULL-by-convention for consumables - enforced in
-- the service layer, not a CHECK, since SQLite can't easily cross-reference
-- sibling columns conditionally without a trigger.
--
-- checkout_session_items.quantity supports a consumable checkout line
-- ("take 3 of these") alongside the existing one-row-per-unit serialized
-- flow; defaults to 1 so every existing/serialized row means exactly what
-- it always has.
ALTER TABLE assets ADD COLUMN asset_type TEXT NOT NULL DEFAULT 'serialized';
ALTER TABLE assets ADD COLUMN quantity INTEGER;
ALTER TABLE assets ADD COLUMN reorder_threshold INTEGER;
CREATE INDEX idx_assets_asset_type ON assets(asset_type);

ALTER TABLE checkout_session_items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1;
