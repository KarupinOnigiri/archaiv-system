-- 0008_add_asset_image.sql
-- Adds an optional photo for each asset. Nullable - every existing asset
-- simply has no image until one is uploaded via the (future) upload UI.
ALTER TABLE assets ADD COLUMN image_path TEXT;
