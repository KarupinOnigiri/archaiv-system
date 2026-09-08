"""Business logic for routes/setup.py: the public "is setup needed" check and the
one-time first-admin creation flow that retires the temporary seeded admin.

(Bisaya: Ang logic sa routes/setup.py - ang public nga check kung
"kinahanglan pa ba mag-setup" ug ang usa-ka-beses ra nga paghimo sa unang
tinuod nga admin account, nga mo-retire/tangtang sa temporary/temp nga
admin nga gi-seed/gibutang.)
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
    """Whether a REAL (not temp-seeded) active admin account exists yet -
    the gate the login page uses to decide whether to send someone to the
    Setup wizard instead.
    (Bisaya: Kung naa na ba'y TINUOD - dili temporary - nga active nga admin
    account. Kini ang gina-check sa login page para mahibaw-an kung
    ipadala ba ang tawo sa Setup wizard imbes.)

    Args:
        conn: an already-open sqlite3.Connection to reuse, or None to open
            (and close) a fresh one.
        (Bisaya: naa nay bukas nga koneksyon nga gamiton pag-usab, o None
        para mag-abre - ug mo-close - ug bag-o.)

    Returns:
        bool - True when setup is still needed (no real active admin
        exists yet).
        (Bisaya: True kung kinahanglan pa gyud mag-setup - kay wala pay
        tinuod nga active nga admin.)
    """
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
    """True only for a brand-new database that has never had any staff at all --
    distinct from is_setup_required(), which is also true after a live system's
    last real admin gets deactivated. That second case (services/seed_service.py's
    recovery path) still goes through the temp-admin-login flow on purpose: a
    populated, already-deployed system shouldn't expose an unauthenticated
    "create yourself an admin account" endpoint to anyone on the LAN. A true
    fresh install has nothing to protect yet, so routes/setup.py skips the temp
    admin entirely and lets complete_setup() below run with temp_admin=None.
    (Bisaya: True ra kung tinuod gyud nga bag-o pa ang database - walay
    gyu'y staff bisan usa - lahi ni sa is_setup_required(), nga True pod
    human ma-deactivate ang katapusang tinuod nga admin sa usa ka
    naa-nang-gigamit nga sistema. Kadtong ikaduhang case (recovery path sa
    seed_service.py) magpadayon gihapon mo-agi sa temp-admin-login flow,
    tuyo gyud - kay ang naa nay data nga sistema dili angay maabli ug
    "himo-og-imong-kaugalingong-admin" nga endpoint nga walay login, sa
    bisan kinsa sa LAN. Ang tinuod nga bag-o/fresh install walay
    proteksyonon pa, mao nga laktawan sa routes/setup.py ang temp admin,
    complete_setup() ra sa ubos dayon ang mo-dagan nga temp_admin=None.

    Args: conn (optional).
    Returns: bool - True kung walay gyud staff sa database.)"""
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
    """temp_admin is None for a true fresh install (no staff exist yet at all --
    see is_fresh_install() and routes/setup.py) - no login required, this
    directly becomes the first admin account. Otherwise temp_admin is the
    currently logged-in temporary setup admin from a recovery re-seed
    (services/seed_service.py, after a live system lost its last real admin),
    and must still be that system-seeded account - retired here once the
    real one exists.
    (Bisaya: Ang temp_admin None kung tinuod nga bag-o/fresh install - walay
    staff gyud - tan-awa ang is_fresh_install() - walay login nga
    gikinahanglan, kini direkta na ang mahimong unang admin account. Kung
    dili, ang temp_admin mao ang naka-login karon nga temporary setup admin
    gikan sa recovery re-seed (seed_service.py, human mawad-an ang naa
    nang sistema sa katapusang tinuod nga admin), ug kinahanglan gihapon
    kining system-seeded nga account - i-retire/tangtangon dinhi human
    mahimo na ang tinuod.

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        data: dict sa setup form - first_name, last_name, department_id,
            username, password, ug uban pa.
        (Bisaya: dict gikan sa setup form - first_name, last_name,
        department_id, username, password, ug uban pa.)
        temp_admin: ang naka-login nga temp admin row, o None kung fresh
            install.
        (Bisaya: ang row sa naka-login nga temp admin, o None kung fresh
        install.)

    Returns:
        tuple (result, error) - `result` naa'y "message" ug "staff_id" kung
        malampuson; `error` naa kung na-block - e.g. na-setup na, dili
        temp admin, o username/barcode nga na-gamit na.
        (Bisaya: usa ka tuple (result, error).)
    """
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
