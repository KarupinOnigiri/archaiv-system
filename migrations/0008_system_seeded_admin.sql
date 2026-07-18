-- 0008_system_seeded_admin.sql
ALTER TABLE staff ADD COLUMN is_system_seeded INTEGER NOT NULL DEFAULT 0;