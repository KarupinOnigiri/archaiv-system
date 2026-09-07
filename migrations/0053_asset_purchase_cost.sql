-- 0053_asset_purchase_cost.sql
-- Optional purchase-cost tracking on assets, for the new Asset Valuation
-- report section (services/stats_service.py's _section_asset_valuation).
-- Nullable on purpose: existing assets won't have this backfilled, and the
-- valuation report/summary tiles only render once at least one asset has a
-- non-null value, so an unfilled fleet just doesn't show the section rather
-- than showing a misleading $0.
ALTER TABLE assets ADD COLUMN purchase_cost NUMERIC;
