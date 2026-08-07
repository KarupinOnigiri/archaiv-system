-- 0026_add_member_phone_number.sql
-- Adds a phone number field to members, surfaced in the Members tab's
-- add/edit forms and in a member's profile modal (routes/members.py,
-- templates/members/*). Nullable -- existing rows and any future member
-- without a phone on file shouldn't be forced to have one.
ALTER TABLE members ADD COLUMN phone_number TEXT;
