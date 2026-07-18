from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from utils.validators import validate_lookup_id, validate_password_strength
from utils.logger import log_action
from werkzeug.security import generate_password_hash
from extensions import limiter
import sqlite3
import logging

logger = logging.getLogger(__name__)

setup_bp = Blueprint("setup", __name__)


@setup_bp.route("/setup/status")
@limiter.limit("30 per minute")
def setup_status():
    """Public, no-auth: lets a kiosk frontend know whether to show a
    'system setup required' screen. Reveals only a boolean — never the
    temp admin's username/password or any other seed detail."""
    conn = get_db_connection()
    active_admin = conn.execute(
        "SELECT id FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 LIMIT 1"
    ).fetchone()
    conn.close()
    return jsonify({"setup_required": active_admin is None})


@setup_bp.route("/setup/complete", methods=["POST"])
@limiter.limit("10 per minute")
@login_required
def complete_setup():
    """Only usable while logged in as the temporary system-seeded admin
    (see seed_service.seed_initial_admin). Creates the real first admin
    account from the given details, then deactivates the temporary
    account and drops its sessions in the same transaction — the
    temporary account can't be reused afterward unless every admin is
    later removed, at which point seed_initial_admin() creates a new one
    on the next startup."""
    if not g.staff["is_system_seeded"]:
        return (
            jsonify(
                {
                    "error": "Setup has already been completed",
                    "reason": "This action is only available while logged in as the temporary setup admin.",
                }
            ),
            403,
        )

    data, error = get_json_or_error()
    if error:
        return error

    required_fields = (
        "first_name",
        "last_name",
        "barcode_id",
        "department_id",
        "username",
        "password",
    )
    missing = [f for f in required_fields if not data.get(f)]
    if missing:
        return (
            jsonify({"error": f"Missing required field(s): {', '.join(missing)}"}),
            400,
        )

    error = validate_password_strength(data.get("password"))
    if error:
        return error

    conn = get_db_connection()

    error = validate_lookup_id(
        conn, "departments", data.get("department_id"), "department_id", required=True
    )
    if error:
        conn.close()
        return error

    temp_admin_id = g.staff["id"]

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
                data["barcode_id"],
                data.get("batch"),
                data["department_id"],
            ),
        )
        member_id = member_cursor.lastrowid
        conn.execute(
            "UPDATE members SET member_code = ? WHERE id = ?",
            (f"MBR-{member_id:05d}", member_id),
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
        conn.execute(
            "UPDATE staff SET staff_code = ? WHERE id = ?",
            (f"STF-{new_staff_id:05d}", new_staff_id),
        )

        # Retire the temporary admin in the same transaction as creating
        # the real one, so there's never a window where both are usable
        # (or, on failure, neither is — the rollback below undoes both
        # halves together).
        conn.execute("UPDATE staff SET is_active = 0 WHERE id = ?", (temp_admin_id,))
        conn.execute("DELETE FROM sessions WHERE staff_id = ?", (temp_admin_id,))

        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError during setup completion: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check that username and barcode_id aren't already in use."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=new_staff_id,
        action="completed_initial_setup",
        target_table="staff",
        target_id=new_staff_id,
        details=(
            f"Created first real admin ({data['username']}) and retired "
            f"the temporary setup admin (staff #{temp_admin_id})"
        ),
    )

    return (
        jsonify(
            {
                "message": "Setup complete. The temporary admin account has been deactivated. Please log in with your new account.",
                "staff_id": new_staff_id,
            }
        ),
        201,
    )
