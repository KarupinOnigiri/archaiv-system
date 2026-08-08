-- 0039_damage_restriction.sql
-- Adds the missing counterpart to the overdue auto-restriction protocol
-- (services/loan_service.py's check_and_restrict_overdue_member): a
-- member who returns `damage_restriction_threshold` or more items in a
-- non-functional condition (transactions.condition_in != 'functional' --
-- inoperable/for_repair/disposed all count) gets auto-restricted too.
-- See services/loan_service.py's check_and_restrict_damaged_returns,
-- called from routes/transactions.py's _finalize_return().
--
-- 'auto_damage' is a NEW, distinct restriction_source from 'auto'
-- (overdue) on purpose: check_and_lift_restriction_if_resolved() only
-- auto-lifts restriction_source = 'auto' rows once a member's overdue
-- items clear. A damage restriction has no equivalent "resolved" signal
-- to auto-detect, so it must stay restricted until a human reviews it
-- through the existing unrestriction-request flow (routes/requests.py),
-- the same as a 'manual' restriction already does -- that flow already
-- lifts source-agnostically, so it needs no changes to support this.
--
-- SQLite can't ALTER a CHECK constraint in place, so members is rebuilt
-- (same pattern as 0006_departments_categories.sql/0021_notification_
-- recipients.sql), preserving every existing row and the member_code
-- unique index.

PRAGMA foreign_keys = OFF;

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
    restriction_source TEXT CHECK (restriction_source IN ('auto', 'manual', 'auto_damage')),
    member_code TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    phone_number TEXT,
    FOREIGN KEY (department_id) REFERENCES departments(id)
);

INSERT INTO members_new (
    id, last_name, first_name, middle_name, suffix, barcode_id, batch,
    department_id, member_type, is_active, is_restricted, restriction_reason,
    restriction_source, member_code, created_at, phone_number
)
SELECT
    id, last_name, first_name, middle_name, suffix, barcode_id, batch,
    department_id, member_type, is_active, is_restricted, restriction_reason,
    restriction_source, member_code, created_at, phone_number
FROM members;

DROP TABLE members;
ALTER TABLE members_new RENAME TO members;
CREATE UNIQUE INDEX idx_members_member_code ON members(member_code);

PRAGMA foreign_keys = ON;

-- Admin-editable occurrence threshold (System page) -- seeded '2' to
-- match the default called out in the request, so nothing changes
-- behaviorally until an admin edits it.
INSERT INTO app_settings (key, value) VALUES ('damage_restriction_threshold', '2');
