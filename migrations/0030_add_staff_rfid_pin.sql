-- 0030_add_staff_rfid_pin.sql
-- Pairs the RFID card (staff.rfid_id, 0029_add_staff_rfid_id.sql) with a
-- short PIN so RFID login is a real second factor -- "something you
-- have" (the card) + "something you know" (the PIN) -- rather than the
-- card alone granting access. Hashed the same way staff.password_hash
-- already is (werkzeug's generate_password_hash/check_password_hash);
-- never stored or returned in plaintext once set, same contract as a
-- password. Nullable: a staff account can have an RFID card on file
-- with no PIN yet, in which case RFID login simply isn't usable for it
-- until one is set (see routes/auth.py: login_rfid).

ALTER TABLE staff ADD COLUMN rfid_pin_hash TEXT;
