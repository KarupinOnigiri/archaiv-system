-- 0004_add_condition_in.sql
-- Adds transactions.condition_in - the condition an asset was recorded as
-- when it came back in on this specific transaction. Distinct from
-- assets.condition, which only reflects the asset's *current* condition
-- and gets overwritten on every borrow/return cycle. This column lets us
-- look back at any past transaction and see what condition it came back
-- in, even after later cycles have moved assets.condition on.
--
-- Nullable: existing 'returned' rows predate this column and have no way
-- to know what condition they came back in retroactively, so they stay
-- NULL rather than being backfilled with a guess.

ALTER TABLE transactions ADD COLUMN condition_in TEXT
    CHECK (condition_in IN ('functional', 'inoperable', 'for_repair', 'disposed'));

    
CREATE INDEX idx_transactions_time_out ON transactions(time_out);