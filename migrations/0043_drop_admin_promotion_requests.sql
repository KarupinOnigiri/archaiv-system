-- 0043_drop_admin_promotion_requests.sql
-- Removes the admin-promotion peer-approval flow added in
-- migrations/0036_admin_promotion_requests.sql. Dropped rather than kept
-- as dead schema: any single admin can already create a brand-new
-- role='admin' staff account directly, with zero approval, via ordinary
-- staff creation (routes/staff.py's add_staff_submit) -- so the
-- peer-approval gate on *promoting an existing staffer* was only ever
-- protecting a door standing wide open right next to it, adding
-- friction without the guarantee it implied. The admin-deactivation
-- peer-approval flow (admin_deactivation_requests, 0005) is unrelated
-- and stays -- it protects against a different, real risk (one admin
-- unilaterally locking out every other admin) that unrestricted admin
-- creation doesn't get around.
DROP TABLE IF EXISTS admin_promotion_approvals;
DROP TABLE IF EXISTS admin_promotion_requests;
