-- 0006_departments_categories.sql
-- Replaces free-text members.department and assets.category with lookup
-- tables, so a department/category must already exist as a canonical
-- entry before it can be attached to a record — eliminates typo-driven
-- fragmentation ("IT" vs "I.T." vs "Info Tech").

PRAGMA foreign_keys = OFF;

CREATE TABLE "departments" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

CREATE TABLE "categories" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL
);

-- Seed from whatever distinct values already exist so nothing is orphaned.
INSERT INTO departments (name) SELECT DISTINCT department FROM members WHERE department IS NOT NULL;
INSERT INTO categories (name) SELECT DISTINCT category FROM assets WHERE category IS NOT NULL;

-- ---- members ----
CREATE TABLE members_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    middle_name TEXT,
    suffix TEXT,
    barcode_id TEXT UNIQUE NOT NULL,
    batch INTEGER,
    department_id INTEGER NOT NULL,
    member_type TEXT NOT NULL CHECK (member_type IN ('trainee', 'trainer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    is_restricted INTEGER NOT NULL DEFAULT 0,
    restriction_reason TEXT,
    restriction_source TEXT CHECK (restriction_source IN ('auto', 'manual')),
    member_code TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

INSERT INTO members_new
SELECT m.id, m.last_name, m.first_name, m.middle_name, m.suffix, m.barcode_id,
       m.batch, d.id, m.member_type, m.is_active, m.is_restricted,
       m.restriction_reason, m.restriction_source, m.member_code, m.created_at
FROM members m
JOIN departments d ON d.name = m.department;

DROP TABLE members;
ALTER TABLE members_new RENAME TO members;
CREATE UNIQUE INDEX idx_members_member_code ON members(member_code);

-- ---- assets ----
CREATE TABLE assets_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    barcode_id TEXT UNIQUE NOT NULL,
    rfid_id TEXT UNIQUE,
    category_id INTEGER,
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'borrowed', 'maintenance', 'reserved')),
    condition TEXT NOT NULL DEFAULT 'functional' CHECK (condition IN ('functional', 'inoperable', 'for_repair', 'disposed')),
    acquired_date DATE,
    last_calibrated TIMESTAMP,
    notes TEXT,
    serial_code TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    deleted_at TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

INSERT INTO assets_new
SELECT a.id, a.asset_id, a.name, a.barcode_id, a.rfid_id, c.id, a.status,
       a.condition, a.acquired_date, a.last_calibrated, a.notes,
       a.serial_code, a.created_at, a.deleted_at
FROM assets a
LEFT JOIN categories c ON c.name = a.category;

DROP TABLE assets;
ALTER TABLE assets_new RENAME TO assets;
CREATE UNIQUE INDEX idx_assets_serial_code ON assets(serial_code);

PRAGMA foreign_keys = ON;