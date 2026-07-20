-- 0009_add_asset_images.sql
-- Adds support for up to 2 additional images per asset, alongside the
-- existing single "main" image (assets.image_path, unchanged).
--
-- Fixed slots (1 and 2) rather than an unbounded gallery -- matches the
-- "main image + a couple of supporting shots" UI, and the UNIQUE
-- constraint lets the app use a single upsert per slot (INSERT ...
-- ON CONFLICT(asset_id, slot) DO UPDATE) instead of separate
-- insert/update code paths.

CREATE TABLE "asset_images" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL,
    slot INTEGER NOT NULL CHECK (slot IN (1, 2)),
    image_path TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (asset_id, slot),
    FOREIGN KEY (asset_id) REFERENCES assets(id)
);

CREATE INDEX idx_asset_images_asset_id ON asset_images(asset_id);
