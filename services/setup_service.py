"""Business logic para sa routes/setup.py - ang public "is setup needed"
check ug ang usa-ka-beses ra nga first-admin creation flow nga mo-retire
sa temporary seeded admin.
"""

import logging

from flask import jsonify
from werkzeug.security import generate_password_hash

from database import get_db_connection, sqlite3
from services.seed_service import purge_temp_admin
from services.settings_service import get_member_code_prefix, get_staff_code_prefix
from utils.logger import log_action
from utils.validators import validate_lookup_id

logger = logging.getLogger(__name__)


def is_setup_required(conn=None):
    """Kung naa na ba'y TINUOD (dili temp-seeded) nga active admin account -
    gina-check sa login page kung ipadala ba ang tawo sa Setup wizard.

    Returns: bool - True kung kinahanglan pa mag-setup."""
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


def is_fresh_install(conn=None):
    """True ra kung tinuod nga bag-o pa ang database - walay staff bisan
    usa - lahi ni sa is_setup_required(), nga True pod human
    ma-deactivate ang katapusang admin sa naa-nay-data nga sistema. Sa
    maong second case, mag-agi gihapon sa temp-admin-login flow (tuyo
    gyud, security reason - dili angay maabli og unauthenticated
    "create-yourself-an-admin" endpoint sa naa-nay-data nga sistema).

    Returns: bool - True kung walay gyud staff sa database."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        staff_count = conn.execute("SELECT COUNT(*) AS cnt FROM staff").fetchone()["cnt"]
        return staff_count == 0
    finally:
        if owns_conn:
            conn.close()


def complete_setup(conn, data, temp_admin):
    """Ang temp_admin None kung tinuod nga fresh install - walay staff
    gyud - walay login nga gikinahanglan, direkta na ni mahimong unang
    admin account. Kung dili, ang temp_admin mao ang naka-login nga temp
    setup admin gikan sa recovery re-seed - i-retire dinhi human mahimo
    na ang tinuod nga admin.

    Returns: tuple (result, error)."""
    if temp_admin is None:
        if not is_fresh_install(conn):
            return None, (
                jsonify(
                    {
                        "error": "Setup has already been completed",
                        "reason": "An admin account already exists; log in instead.",
                    }
                ),
                403,
            )
    elif not temp_admin["is_system_seeded"]:
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

    temp_admin_id = temp_admin["id"] if temp_admin else None
    temp_admin_member_id = temp_admin["member_id"] if temp_admin else None

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
        generated_member_code = f"{get_member_code_prefix(conn)}{member_id:05d}"
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
        conn.execute("UPDATE staff SET staff_code = ? WHERE id = ?", (f"{get_staff_code_prefix(conn)}{new_staff_id:05d}", new_staff_id))

        # Retires the temp admin in the same transaction, so there's never a window with both.
        # Fully deleted, not soft-deactivated, per product decision - shouldn't linger anywhere.
        # Skipped entirely on a fresh install - there was never a temp admin to retire.
        if temp_admin_id is not None:
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
        conn=conn,
        staff_id=new_staff_id,
        action="completed_initial_setup",
        target_table="staff",
        target_id=new_staff_id,
        target_name=data["username"],
        details=(
            f"Created the first admin account ({data['username']}) on a fresh install"
            if temp_admin_id is None
            else (
                f"Created first real admin ({data['username']}) and retired "
                f"the temporary setup admin (staff #{temp_admin_id})"
            )
        ),
    )

    return {
        "message": "Setup complete. Please log in with your new account.",
        "staff_id": new_staff_id,
    }, None
