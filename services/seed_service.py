import secrets
import sqlite3
import logging

from database import get_db_connection
from utils.logger import log_action
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

TEMP_ADMIN_USERNAME = "setup-admin"
TEMP_ADMIN_DEPARTMENT = "Administration"


def _get_or_create_department_id(conn, name):
    dept_row = conn.execute(
        "SELECT id FROM departments WHERE name = ?", (name,)
    ).fetchone()
    if dept_row is not None:
        return dept_row["id"]
    cursor = conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _create_temp_admin(conn):
    """Creates a brand-new temporary admin: a fresh member + staff row,
    role='admin', is_system_seeded=1, with a freshly generated random
    password. Returns (staff_id, password) so the caller can log it."""
    department_id = _get_or_create_department_id(conn, TEMP_ADMIN_DEPARTMENT)

    # Unique per (re)seed — the member row from a *previous* temp admin
    # still exists (soft-retired, not deleted), so its barcode_id is
    # already taken and a new one is needed here.
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


def _regenerate_temp_admin_password(conn, staff_row):
    """A temp admin already exists from a previous boot — setup was never
    completed. Rather than let last boot's printed password stay valid
    forever, issue a fresh one on every startup and drop any sessions
    signed in with the old one, so an old log line can't still get
    someone in."""
    password = secrets.token_urlsafe(12)
    hashed_password = generate_password_hash(password)
    conn.execute(
        "UPDATE staff SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (hashed_password, staff_row["id"]),
    )
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_row["id"],))
    return password


def seed_initial_admin():
    """Ensures the system always has a way in, without ever shipping a
    fixed default username/password.

    If any active admin account exists, this is a no-op. Otherwise, a
    temporary admin account is created — or, if one already exists from
    an incomplete previous setup, given a fresh password — and its
    one-time credentials are logged (stdout + toolroom.log, via the
    logging config already set up in app.py). Logging in with that
    account and calling POST /api/v1/setup/complete creates the real
    first admin and retires the temporary one automatically.

    Safe to call at any time — on app startup, and also whenever an
    action might have just removed the last active admin (e.g. an
    admin-deactivation approval finalizing)."""
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(
            "No active admin account exists and the temporary setup admin "
            f"could not be (re)seeded — a database constraint was violated: {e}"
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
        details="No active admin found — (re)seeded the temporary setup admin account",
    )

    logger.info(
        "No active admin account found. A temporary setup admin is ready — "
        f"username='{username}' password='{password}'. Log in with these "
        "credentials and call POST /api/v1/setup/complete to create the "
        "real first admin account; the temporary account will then be "
        "deactivated automatically. This password will not be shown again."
    )
