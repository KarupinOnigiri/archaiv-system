"""Business logic for routes/setup.py: the public "is setup needed" check and the
one-time first-admin creation flow that retires the temporary seeded admin.
"""

import logging

from flask import jsonify
from werkzeug.security import generate_password_hash

from database import get_db_connection, sqlite3
from services.seed_service import purge_temp_admin
from utils.logger import log_action
from utils.validators import validate_lookup_id

logger = logging.getLogger(__name__)


def is_setup_required(conn=None):
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        active_admin = conn.execute(
            "SELECT id FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 LIMIT 1"
        ).fetchone()
        return active_admin is None
    finally:
        if owns_conn:
            conn.close()


def complete_setup(conn, data, temp_admin):
    """Only callable as the temp admin; creates the real one and retires the temp."""
    if not temp_admin["is_system_seeded"]:
        return None, (
            jsonify(
                {
                    "error": "Setup has already been completed",
                    "reason": "This action is only available while logged in as the temporary setup admin.",
                }
            ),
            403,
        )

    error = validate_lookup_id(conn, "departments", data.get("department_id"), "department_id", required=True)
    if error:
        return None, error

    temp_admin_id = temp_admin["id"]
    temp_admin_member_id = temp_admin["member_id"]

    try:
        member_cursor = conn.execute(
            """
            INSERT INTO members
                (first_name, last_name, middle_name, suffix, barcode_id, batch, department_id, member_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'trainer')
            """,
            (
                data["first_name"],
                data["last_name"],
                data.get("middle_name"),
                data.get("suffix"),
                "",
                data.get("batch"),
                data["department_id"],
            ),
        )
        member_id = member_cursor.lastrowid
        generated_member_code = f"MBR{member_id:05d}"
        conn.execute(
            "UPDATE members SET member_code = ?, barcode_id = ? WHERE id = ?",
            (generated_member_code, generated_member_code, member_id),
        )

        hashed_password = generate_password_hash(data["password"])
        staff_cursor = conn.execute(
            """
            INSERT INTO staff (member_id, username, password_hash, role, is_active, is_system_seeded)
            VALUES (?, ?, ?, 'admin', 1, 0)
            """,
            (member_id, data["username"], hashed_password),
        )
        new_staff_id = staff_cursor.lastrowid
        conn.execute("UPDATE staff SET staff_code = ? WHERE id = ?", (f"STF{new_staff_id:05d}", new_staff_id))

        # Retires the temp admin in the same transaction, so there's never a window with both.
        # Fully deleted, not soft-deactivated, per product decision -- shouldn't linger anywhere.
        purge_temp_admin(conn, temp_admin_id, temp_admin_member_id)

        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError during setup completion: {e}")
        return None, (
            jsonify(
                {
                    "error": "A database constraint was violated. Check that username and barcode_id aren't already in use."
                }
            ),
            400,
        )

    log_action(
        staff_id=new_staff_id,
        action="completed_initial_setup",
        target_table="staff",
        target_id=new_staff_id,
        target_name=data["username"],
        details=(
            f"Created first real admin ({data['username']}) and retired "
            f"the temporary setup admin (staff #{temp_admin_id})"
        ),
    )

    return {
        "message": "Setup complete. The temporary admin account has been deactivated. Please log in with your new account.",
        "staff_id": new_staff_id,
    }, None
