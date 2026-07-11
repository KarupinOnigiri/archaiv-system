import os
from database import get_db_connection
from utils.logger import log_action
from werkzeug.security import generate_password_hash
import sqlite3


def seed_initial_admin():
    """Creates the first admin account from .env values, but only if
    no admin account exists yet. Safe to call on every app startup —
    it's a no-op once an admin exists."""
    conn = get_db_connection()

    existing_admin = conn.execute(
        "SELECT id FROM staff WHERE role = 'admin' LIMIT 1"
    ).fetchone()

    if existing_admin:
        conn.close()
        return

    username = os.environ.get("ADMIN_USERNAME")
    password = os.environ.get("ADMIN_PASSWORD")
    first_name = os.environ.get("ADMIN_FIRST_NAME")
    last_name = os.environ.get("ADMIN_LAST_NAME")
    middle_name = os.environ.get("ADMIN_MIDDLE_NAME")
    suffix = os.environ.get("ADMIN_SUFFIX")
    barcode_id = os.environ.get("ADMIN_BARCODE_ID")
    department = os.environ.get("ADMIN_DEPARTMENT", "Administration")

    missing = [
        var_name
        for var_name, value in (
            ("ADMIN_USERNAME", username),
            ("ADMIN_PASSWORD", password),
            ("ADMIN_FIRST_NAME", first_name),
            ("ADMIN_LAST_NAME", last_name),
            ("ADMIN_BARCODE_ID", barcode_id),
        )
        if not value
    ]

    if missing:
        conn.close()
        print(
            "No admin account exists and initial admin could not be seeded — "
            f"missing .env values: {', '.join(missing)}"
        )
        return

    try:
        member_cursor = conn.execute(
            """
            INSERT INTO members (first_name, last_name, middle_name, suffix, barcode_id, department, member_type)
            VALUES (?, ?, ?, ?, ?, ?, 'trainer')
            """,
            (first_name, last_name, middle_name, suffix, barcode_id, department),
        )
        member_id = member_cursor.lastrowid

        hashed_password = generate_password_hash(password)

        staff_cursor = conn.execute(
            """
            INSERT INTO staff (member_id, username, password_hash, role, is_active)
            VALUES (?, ?, ?, 'admin', 1)
            """,
            (member_id, username, hashed_password),
        )
        staff_id = staff_cursor.lastrowid

        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(
            "No admin account exists and initial admin could not be seeded — "
            f"a database constraint was violated: {e}. "
            "Check that ADMIN_USERNAME and ADMIN_BARCODE_ID in .env don't already "
            "belong to an existing staff/member record, then restart the app."
        )
        return
    finally:
        conn.close()

    log_action(
        staff_id=staff_id,
        action="seeded_initial_admin",
        target_table="staff",
        target_id=staff_id,
        details=f"Auto-seeded initial admin account: {username}",
    )

    print(f"Initial admin account created: username='{username}'")
