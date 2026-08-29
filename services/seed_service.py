import json
import secrets
import logging

# Re-exported from database.py, not stdlib -- see routes/assets.py's import for why.
from database import get_db_connection, sqlite3
from utils.logger import log_action
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

TEMP_ADMIN_USERNAME = "setup-admin"
TEMP_ADMIN_DEPARTMENT = "Administration"

# Stashes the one-time temp-admin credentials so the login page can show them in a modal.
SEED_NOTICE_SETTING_KEY = "setup_admin_seed_notice"


def _set_seed_notice(conn, username, password):
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SEED_NOTICE_SETTING_KEY, json.dumps({"username": username, "password": password})),
    )


def get_seed_notice(conn=None):
    """Returns {"username", "password"} for the pending temp admin, or None once setup's done."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (SEED_NOTICE_SETTING_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row["value"])
        except (ValueError, TypeError):
            return None
    finally:
        if owns_conn:
            conn.close()


def clear_seed_notice(conn):
    conn.execute("DELETE FROM app_settings WHERE key = ?", (SEED_NOTICE_SETTING_KEY,))


def _get_or_create_department_id(conn, name):
    dept_row = conn.execute(
        "SELECT id FROM departments WHERE name = ?", (name,)
    ).fetchone()
    if dept_row is not None:
        return dept_row["id"]
    cursor = conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _create_temp_admin(conn):
    """Creates a temp admin (member + staff row, role='admin') with a random password."""
    department_id = _get_or_create_department_id(conn, TEMP_ADMIN_DEPARTMENT)

    # Unique per reseed -- a previous temp admin's barcode_id is already taken.
    barcode_id = f"SETUP{secrets.token_hex(4).upper()}"
    password = secrets.token_urlsafe(12)
    hashed_password = generate_password_hash(password)

    member_cursor = conn.execute(
        """
        INSERT INTO members (first_name, last_name, barcode_id, department_id, member_type)
        VALUES ('Temporary', 'Setup Admin', ?, ?, 'trainer')
        """,
        (barcode_id, department_id),
    )
    member_id = member_cursor.lastrowid
    conn.execute(
        "UPDATE members SET member_code = ? WHERE id = ?",
        (f"MBR{member_id:05d}", member_id),
    )

    staff_cursor = conn.execute(
        """
        INSERT INTO staff (member_id, username, password_hash, role, is_active, is_system_seeded)
        VALUES (?, ?, ?, 'admin', 1, 1)
        """,
        (member_id, TEMP_ADMIN_USERNAME, hashed_password),
    )
    staff_id = staff_cursor.lastrowid
    conn.execute(
        "UPDATE staff SET staff_code = ? WHERE id = ?",
        (f"STF{staff_id:05d}", staff_id),
    )

    return staff_id, password


def purge_temp_admin(conn, staff_id, member_id):
    """Fully removes the temp admin's rows instead of soft-deactivating, per product decision.
    Nullable FKs get nulled out; rows only relevant to this staff member get deleted."""
    conn.execute("UPDATE audit_log SET staff_id = NULL WHERE staff_id = ?", (staff_id,))
    conn.execute(
        "UPDATE staff SET created_by_staff_id = NULL WHERE created_by_staff_id = ?",
        (staff_id,),
    )
    conn.execute("UPDATE assets SET deleted_by = NULL WHERE deleted_by = ?", (staff_id,))
    conn.execute(
        "UPDATE checkout_sessions SET staff_id = NULL WHERE staff_id = ?", (staff_id,)
    )
    conn.execute("DELETE FROM notification_reads WHERE staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM notifications WHERE recipient_staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,))
    conn.execute("DELETE FROM staff WHERE id = ?", (staff_id,))
    conn.execute("DELETE FROM members WHERE id = ?", (member_id,))
    clear_seed_notice(conn)


def _regenerate_temp_admin_password(conn, staff_row):
    """Issues a fresh password every boot and drops sessions signed in with the old one."""
    password = secrets.token_urlsafe(12)
    hashed_password = generate_password_hash(password)
    conn.execute(
        "UPDATE staff SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (hashed_password, staff_row["id"]),
    )
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_row["id"],))
    return password


def seed_initial_admin():
    """Ensures the system always has a way in, without a fixed default username/password.
    No-op if an active admin exists; otherwise (re)seeds a temp admin and logs its credentials."""
    conn = get_db_connection()

    active_admin = conn.execute(
        "SELECT id FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 LIMIT 1"
    ).fetchone()

    if active_admin:
        conn.close()
        return

    existing_temp_admin = conn.execute("""
        SELECT * FROM staff
        WHERE is_system_seeded = 1 AND is_active = 1
        ORDER BY id DESC LIMIT 1
        """).fetchone()

    try:
        if existing_temp_admin is not None:
            password = _regenerate_temp_admin_password(conn, existing_temp_admin)
            staff_id = existing_temp_admin["id"]
            username = existing_temp_admin["username"]
        else:
            staff_id, password = _create_temp_admin(conn)
            username = TEMP_ADMIN_USERNAME
        _set_seed_notice(conn, username, password)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(
            "No active admin account exists and the temporary setup admin "
            f"could not be (re)seeded - a database constraint was violated: {e}"
        )
        return
    finally:
        conn.close()

    log_action(
        staff_id=staff_id,
        action="seeded_temporary_admin",
        target_table="staff",
        target_id=staff_id,
        target_name=username,
        details="No active admin found - (re)seeded the temporary setup admin account",
    )

    logger.info(
        "No active admin account found. A temporary setup admin is ready - "
        f"username='{username}' password='{password}'. Log in with these "
        "credentials and call POST /api/v1/setup/complete to create the "
        "real first admin account; the temporary account will then be "
        "deactivated automatically. This password will not be shown again."
    )
