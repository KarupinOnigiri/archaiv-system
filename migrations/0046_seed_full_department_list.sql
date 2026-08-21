-- 0046_seed_full_department_list.sql
-- Before this, a fresh install's departments table only ever picked up
-- "Administration" (created as a side effect of seeding the temporary
-- setup admin -- see services/seed_service.py's TEMP_ADMIN_DEPARTMENT)
-- plus whatever an admin happened to add by hand through the Manage
-- Departments modal. New/reset deployments should start with the
-- college's full department list already in place instead of just that
-- one entry.
--
-- INSERT OR IGNORE (on the existing UNIQUE(name) constraint) makes this
-- safe to run against a database that already has some of these rows
-- (added manually before this migration existed) -- it only fills in
-- whatever's missing, never duplicates or renames an existing row.
--
-- sort_order continues on from the current max so newly-added rows land
-- after any existing ones rather than colliding at 0 -- the Manage
-- Departments modal lets an admin reorder them afterward regardless.
INSERT OR IGNORE INTO departments (name, sort_order)
SELECT v.name,
       (SELECT COALESCE(MAX(sort_order), -1) FROM departments) + v.rn
FROM (
    SELECT 1 AS rn, 'Administration' AS name
    UNION ALL SELECT 2, 'DABET'
    UNION ALL SELECT 3, 'DIT'
    UNION ALL SELECT 4, 'DIAMT'
    UNION ALL SELECT 5, 'DCET'
    UNION ALL SELECT 6, 'DAT'
    UNION ALL SELECT 7, 'DHRT'
    UNION ALL SELECT 8, 'DMET-M'
    UNION ALL SELECT 9, 'DMET-W'
    UNION ALL SELECT 10, 'DHVAC'
) AS v;
