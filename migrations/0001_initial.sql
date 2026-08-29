-- 0001_initial.sql
-- Baseline schema. This is a snapshot of schema.sql at the point migrations
-- were introduced. For installs that already have a database (schema
-- already applied the old way), database.py detects that and records this
-- migration as "already applied" without re-running it - see init_db().

CREATE TABLE "assets" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    barcode_id TEXT UNIQUE NOT NULL,
    rfid_id TEXT UNIQUE,
    category TEXT,
    status TEXT NOT NULL DEFAULT 'available' CHECK (status IN ('available', 'borrowed', 'maintenance', 'reserved')),
    condition TEXT NOT NULL DEFAULT 'functional' CHECK (condition IN ('functional', 'inoperable', 'for_repair', 'disposed')),
    acquired_date DATE,
    last_calibrated TIMESTAMP,
    notes TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    deleted_at TIMESTAMP
);

CREATE TABLE "members" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_name TEXT NOT NULL,
    first_name TEXT NOT NULL,
    middle_name TEXT,
    suffix TEXT,
    barcode_id TEXT UNIQUE NOT NULL,
    batch INTEGER,
    department TEXT NOT NULL,
    member_type TEXT NOT NULL CHECK (member_type IN ('trainee', 'trainer')),
    is_active INTEGER NOT NULL DEFAULT 1,
    is_restricted INTEGER NOT NULL DEFAULT 0,
    restriction_reason TEXT,
    restriction_source TEXT CHECK (restriction_source IN ('auto', 'manual')),
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
);

CREATE TABLE "checkout_sessions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'completed', 'cancelled')),
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    completed_at TIMESTAMP,
    FOREIGN KEY (member_id) REFERENCES "members"(id)
);

CREATE TABLE "checkout_session_items" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkout_session_id INTEGER NOT NULL,
    asset_id INTEGER NOT NULL,
    added_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    UNIQUE (checkout_session_id, asset_id),
    FOREIGN KEY (checkout_session_id) REFERENCES checkout_sessions(id),
    FOREIGN KEY (asset_id) REFERENCES assets(id)
);

CREATE TABLE "transactions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checkout_session_id INTEGER,
    asset_id INTEGER NOT NULL,
    member_id INTEGER NOT NULL,
    borrowed_by_staff_id INTEGER,
    returned_by_staff_id INTEGER,
    time_out TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    due_date TIMESTAMP,
    condition_out TEXT CHECK (condition_out IN ('functional', 'inoperable', 'for_repair', 'disposed')),
    time_in TIMESTAMP,
    return_remarks TEXT,
    status TEXT NOT NULL DEFAULT 'borrowed' CHECK (status IN ('borrowed', 'returned')),
    FOREIGN KEY (checkout_session_id) REFERENCES checkout_sessions(id),
    FOREIGN KEY (asset_id) REFERENCES "assets"(id),
    FOREIGN KEY (member_id) REFERENCES "members"(id),
    FOREIGN KEY (borrowed_by_staff_id) REFERENCES staff(id),
    FOREIGN KEY (returned_by_staff_id) REFERENCES staff(id)
);

CREATE TABLE "audit_log" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER,
    action TEXT NOT NULL,
    target_table TEXT,
    target_id INTEGER,
    details TEXT,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (staff_id) REFERENCES staff(id)
);

CREATE TABLE "staff" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id INTEGER UNIQUE NOT NULL,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'in_charge', 'tool_keeper')),
    assigned_start DATE,
    assigned_end DATE,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_by_staff_id INTEGER,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    locked_until TIMESTAMP,
    failed_login_attempts INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (member_id) REFERENCES "members"(id),
    FOREIGN KEY (created_by_staff_id) REFERENCES staff(id)
);

CREATE TABLE "sessions" (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    csrf_token TEXT NOT NULL DEFAULT '',
    staff_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    last_active_at TIMESTAMP DEFAULT (datetime('now', 'localtime')),
    expires_at TIMESTAMP NOT NULL,
    FOREIGN KEY (staff_id) REFERENCES staff(id)
);

CREATE INDEX idx_transactions_status ON transactions(status);
CREATE INDEX idx_transactions_due_date ON transactions(due_date);
CREATE INDEX idx_transactions_member_id ON transactions(member_id);
CREATE INDEX idx_transactions_asset_id ON transactions(asset_id);
