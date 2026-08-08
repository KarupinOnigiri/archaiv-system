-- 0036_add_default_loan_days.sql
-- Admin-editable default loan duration for the System page (see
-- services/settings_service.py's get_default_loan_days,
-- routes/staff.py's loan-duration manage-form/update pair). Replaces
-- the previously-hardcoded DEFAULT_LOAN_DAYS = 2 constant in
-- routes/transactions.py's checkout flow -- that constant now only
-- backs DEFAULT_SETTINGS as a fallback if this row is ever missing.
-- Seeded '2' so nothing changes behaviorally until an admin edits it.
INSERT INTO app_settings (key, value) VALUES ('default_loan_days', '2');
