-- 0028_resync_codes_and_barcodes.sql
-- Two changes, applied to every existing row (confirmed safe to do -- no
-- barcode has been printed/deployed on a physical item or ID card yet;
-- this is still test/dev data):
--
--   1. Drop the dash from every generated reference code:
--      AST-00001 -> AST00001, MBR-00001 -> MBR00001, STF-00001 -> STF00001.
--
--   2. Sync barcode_id to match the record's own code, for uniformity --
--      previously barcode_id was a separate, manually-typed field (see
--      routes/assets.py and routes/members.py before this migration) that
--      could be anything a staff member entered. From here on, the
--      application always auto-generates barcode_id to equal the record's
--      own code at creation time (see add_asset_submit / add_member_submit
--      / import_assets_csv / import_members_csv / complete_setup), so this
--      brings every existing row in line with that same guarantee.
--
-- staff has no barcode_id of its own -- a staff account's barcode is
-- always its linked member's barcode_id (staff.member_id), which the
-- members UPDATE below already covers.

UPDATE assets
SET asset_id = 'AST' || printf('%05d', id),
    barcode_id = 'AST' || printf('%05d', id);

UPDATE members
SET member_code = 'MBR' || printf('%05d', id),
    barcode_id = 'MBR' || printf('%05d', id);

UPDATE staff
SET staff_code = 'STF' || printf('%05d', id);
