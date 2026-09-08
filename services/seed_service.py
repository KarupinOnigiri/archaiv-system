import json
import secrets
import logging

# Re-exported from database.py, not stdlib - see routes/assets.py's import for why.
from database import get_db_connection, sqlite3
from utils.logger import log_action
from werkzeug.security import generate_password_hash

logger = logging.getLogger(__name__)

TEMP_ADMIN_USERNAME = "setup-admin"
TEMP_ADMIN_DEPARTMENT = "Administration"

# Stashes the one-time temp-admin credentials so the login page can show them in a modal.
SEED_NOTICE_SETTING_KEY = "setup_admin_seed_notice"


def _set_seed_notice(conn, username, password):
    """Stashes the just-generated temp admin's credentials so the login page
    can show them once (via get_seed_notice below), then clear_seed_notice()
    removes them for good.
    (Bisaya: I-store una ang credentials sa bag-ong temp admin, para makita
    kausa ra sa login page - pinaagi sa get_seed_notice sa ubos - dayon
    tangtangon na dayon sa clear_seed_notice() aron dili na makita pag-usab.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        username: the temp admin's username.
        (Bisaya: username sa temp admin.)
        password: the temp admin's plain-text one-time password (only ever
            stored here temporarily, until the notice is cleared).
        (Bisaya: ang plain-text nga password sa temp admin - usa ra ka
        beses gamiton - gi-store lang ni dinhi temporarily hangtod
        tangtangon.)

    Returns:
        None - caller is responsible for conn.commit().
        (Bisaya: walay gibalik - ang nagtawag ang responsable mag-commit.)
    """
    conn.execute(
        """
        INSERT INTO app_settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (SEED_NOTICE_SETTING_KEY, json.dumps({"username": username, "password": password})),
    )


def get_seed_notice(conn=None):
    """Returns {"username", "password"} for the pending temp admin, or None once setup's done.
    (Bisaya: Ibalik ang {"username", "password"} sa temp admin nga naghulat
    pa i-setup, o None human na-human na ang setup.)

    Args:
        conn: an already-open sqlite3.Connection to reuse, or None to open
            (and close) a fresh one.
        (Bisaya: naa nay bukas nga koneksyon nga gamiton pag-usab, o None
        para mag-abre - ug mo-close - ug bag-o.)

    Returns:
        dict {"username": str, "password": str} or None.
        (Bisaya: dict {"username": str, "password": str} o None.)
    """
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
    """Removes the stashed temp-admin credentials for good, once they've
    been shown to whoever logged in as the temp admin.
    (Bisaya: Tangtangon sa hingpit ang na-store nga credentials sa temp
    admin, human na kini makita sa nag-login isip temp admin.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        None - caller is responsible for conn.commit().
        (Bisaya: walay gibalik - ang nagtawag ang responsable mag-commit.)
    """
    conn.execute("DELETE FROM app_settings WHERE key = ?", (SEED_NOTICE_SETTING_KEY,))


def _get_or_create_department_id(conn, name):
    """Finds a department by name, creating it if it doesn't exist yet.
    (Bisaya: Pangitaon ang department base sa ngalan, himoon kung wala pa
    ni.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        name: the department's exact name to find/create.
        (Bisaya: eksakto nga ngalan sa department nga pangitaon/himoon.)

    Returns:
        int - that department's id.
        (Bisaya: numero - ang id sa maong department.)
    """
    dept_row = conn.execute(
        "SELECT id FROM departments WHERE name = ?", (name,)
    ).fetchone()
    if dept_row is not None:
        return dept_row["id"]
    cursor = conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
    return cursor.lastrowid


def _create_temp_admin(conn):
    """Creates a temp admin (member + staff row, role='admin') with a random password.
    (Bisaya: Mo-himo ug temp admin - member row + staff row, role='admin' -
    naay random nga password.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        tuple (staff_id, password) - the new temp admin's staff id and
        plain-text one-time password (caller is responsible for showing
        this once and never persisting it in plain text elsewhere).
        (Bisaya: usa ka tuple (staff_id, password) - ang id sa bag-ong
        temp admin ug ang plain-text nga password nga usa ra ka beses
        gamiton - ang nagtawag ang responsable magpakita ani kausa ra ug
        dili gyud i-store sa lain nga lugar nga plain-text.)
    """
    department_id = _get_or_create_department_id(conn, TEMP_ADMIN_DEPARTMENT)

    # Unique per reseed - a previous temp admin's barcode_id is already taken.
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
    Nullable FKs get nulled out; rows only relevant to this staff member get deleted.
    (Bisaya: TANGTANGON GYUD sa hingpit ang mga row sa temp admin, imbes
    i-soft-deactivate lang - desisyon gyud ni sa produkto/system. Ang mga
    column nga pwede walay laman (nullable foreign key) i-set lang sa
    NULL; ang mga row nga para ra gyud niining staff i-delete gyud.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff_id: id of the temp admin's staff row to purge.
        (Bisaya: id sa staff row sa temp admin nga tangtangon.)
        member_id: id of the temp admin's member row to purge.
        (Bisaya: id sa member row sa temp admin nga tangtangon.)

    Returns:
        None - caller is responsible for conn.commit().
        (Bisaya: walay gibalik - ang nagtawag ang responsable mag-commit.)
    """
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
    """Issues a fresh password every boot and drops sessions signed in with the old one.
    (Bisaya: Mo-himo ug bag-ong password matag pag-boot/restart sa app, ug
    i-tangtang ang mga session nga naka-login gamit ang daan nga password -
    para dili na magamit pa ang daan.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff_row: the temp admin's existing staff row.
        (Bisaya: ang naa nang staff row sa temp admin.)

    Returns:
        str - the fresh plain-text password.
        (Bisaya: usa ka string - ang bag-ong plain-text nga password.)
    """
    password = secrets.token_urlsafe(12)
    hashed_password = generate_password_hash(password)
    conn.execute(
        "UPDATE staff SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (hashed_password, staff_row["id"]),
    )
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_row["id"],))
    return password


def seed_initial_admin():
    """Ensures a LIVE system always has a way back in after losing its last real
    admin, without a fixed default username/password. No-op if an active admin
    exists.

    Deliberately does NOT seed a temp admin for a true fresh install (staff
    table completely empty) - that case is handled by routes/setup.py serving
    an unauthenticated "create the first admin" form directly instead (see
    services/setup_service.py's is_fresh_install/complete_setup). A temp
    admin with a server-logged password only makes sense once the system is
    actually in use and has something worth protecting from an open LAN
    enrollment endpoint; a brand-new, never-configured database doesn't.
    (Bisaya: Kini nga function ang naghatag TIKANG/access balik sa usa ka
    BUHI/live nga sistema human mawala ang katapusan niyang tinuod nga
    admin, wala nay fixed/permanente nga default username/password.
    Walay buhaton kung naa nay active nga admin.

    Tuyo gyud nga DILI mag-seed ug temp admin para sa tinuod nga
    fresh install - walay staff gyud - kay kadto nga case gina-handle sa
    routes/setup.py mismo, nag-serve ug walay-login nga "himo-og-unang-
    admin" nga form direkta - tan-awa ang setup_service.py's
    is_fresh_install/complete_setup. Ang temp admin nga naay
    password nga gi-log sa server makahatag lang og sense kung ang sistema
    naa nay tinuod nga gina-gamit ug naay bahandi nga proteksyonan gikan
    sa bukas nga LAN enrollment endpoint; ang bag-ong database nga wala pa
    gyud gi-set-up walay ipanalipdan pa.

    Walay parameter ug walay return value - gitawag ni sa pag-start sa
    app.)"""
    conn = get_db_connection()

    active_admin = conn.execute(
        "SELECT id FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 LIMIT 1"
    ).fetchone()

    if active_admin:
        conn.close()
        return

    staff_count = conn.execute("SELECT COUNT(*) AS cnt FROM staff").fetchone()["cnt"]
    if staff_count == 0:
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
