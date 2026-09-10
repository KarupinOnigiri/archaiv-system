"""Login/session-establishment logic para sa routes/auth.py
(username+password, barcode+PIN, logout). Matag function mo-dawat ug bukas
nga `conn` (gawas sa start_session), mobalik ug plain data - DILI Flask
response - para full control sa routes/auth.py kung unsaon pagpakita ang
error (JSON o redirect).

staff.rfid_pin_hash's name is a leftover from when RFID+PIN was the
quick-login method - that login route (routes/auth.py:login_rfid) has
since been removed entirely in favor of barcode+PIN below, which reuses
the same column rather than adding a new one. find_admin_for_rfid_login()
also still reads staff.rfid_id, but that one stays genuinely relevant -
it's how the Master Key Card deactivation flow (routes/monitoring.py)
identifies which admin tapped their card, unrelated to login PINs.

Convention: `(data, error)` - `error` None kung malampuson, o
`(payload_dict, status)` tuple kung napakyas.
"""

import secrets
from datetime import datetime, timedelta

from database import get_db_connection
from services.settings_service import (
    get_session_idle_minutes,
    get_session_absolute_hours,
    get_max_failed_attempts,
    get_lockout_minutes,
    INDEFINITE_LOCK_UNTIL,
)
from utils.logger import log_action
from utils.validators import decimal_scan_hex_suffix


def check_existing_session_conflict(conn, token):
    """Single-session-per-device policy - i-block ang login kung naay
    buhi/live nga session pa niining browser, bisan kang kinsa. Kung
    stale na (expired/timeout), hinlo-an ra, dili i-block.

    Returns: tuple (blocked, payload_or_None)."""
    if not token:
        return False, None

    existing_session = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if existing_session is None:
        return False, None

    now = datetime.now()
    expires_at = datetime.strptime(existing_session["expires_at"], "%Y-%m-%d %H:%M:%S")
    last_active_at = datetime.strptime(existing_session["last_active_at"], "%Y-%m-%d %H:%M:%S")
    session_is_live = now <= expires_at and (now - last_active_at) <= timedelta(
        minutes=get_session_idle_minutes(conn)
    )

    if session_is_live:
        current_staff = conn.execute(
            "SELECT username FROM staff WHERE id = ?", (existing_session["staff_id"],)
        ).fetchone()
        return True, {
            "error": "Another account is already logged in on this device. Please log out first.",
            "current_username": current_staff["username"] if current_staff else None,
        }

    # stale cookie (expired/timed out) - just clean it up, not a real block
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    return False, None


def find_staff_for_login(conn, username, barcode_id):
    """Pangitaon una base sa username, kung wala makit-i sulayan isip
    barcode; o barcode_id direkta kung gipasa na.

    Returns: sqlite3.Row (staff + member info) o None."""
    if username:
        staff = conn.execute(
            """
            SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
            FROM staff
            JOIN members ON staff.member_id = members.id
            WHERE staff.username = ?
            """,
            (username,),
        ).fetchone()
        # Retries as a barcode if the username lookup misses, unless barcode_id was already passed.
        if staff is None and not barcode_id:
            staff = conn.execute(
                """
                SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
                FROM staff
                JOIN members ON staff.member_id = members.id
                WHERE UPPER(members.barcode_id) = UPPER(?)
                """,
                (username,),
            ).fetchone()
        return staff

    return conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(members.barcode_id) = UPPER(?)
        """,
        (barcode_id,),
    ).fetchone()


def find_admin_for_rfid_login(conn, rfid_id):
    """Pangitaon ang admin base sa rfid_id - dili na para sa RFID+PIN login
    flow (gikuha na, gipulihan sa Barcode+PIN - see templates/login.html's
    comment), kani nga function nabilin lang kay gigamit gihapon sa
    routes/monitoring.py's deactivate_rfid_alarm(): i-identify kang
    kinsang admin ang nag-tap sa Master Key Card sa tool room's HF/NFC
    reader para ma-clear ang active alarm.

    Returns: sqlite3.Row o None."""
    # Some readers set to type scans as keyboard input only type the decimal
    # value of an EPC's last 3 bytes, not the full hex ID - see
    # decimal_scan_hex_suffix()'s docstring. That extra LIKE branch is a
    # harmless no-op when rfid_id isn't a plausible decimal scan.
    return conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE (UPPER(staff.rfid_id) = UPPER(?) OR UPPER(staff.rfid_id) LIKE '%' || ?) AND staff.role = 'admin'
        """,
        (rfid_id, decimal_scan_hex_suffix(rfid_id)),
    ).fetchone()


def find_staff_for_barcode_pin_login(conn, barcode_id):
    """Pareho ka porma sa find_admin_for_rfid_login, apan base sa
    barcode_id ug bukas sa TANANG role, dili admin ra - kini ang tinuod
    nga gigamit karon sa login page.

    Returns: sqlite3.Row o None."""
    return conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(members.barcode_id) = UPPER(?)
        """,
        (barcode_id,),
    ).fetchone()


def find_staff_for_barcode_pin_lookup(conn, barcode_id):
    """I-identify kang kinsang barcode ang gi-scan, una i-hangyo ang PIN.

    Returns: sqlite3.Row (limited columns) o None."""
    return conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.rfid_pin_hash, staff.barcode_pin_enabled, staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(members.barcode_id) = UPPER(?)
        """,
        (barcode_id,),
    ).fetchone()


def find_staff_for_username_lookup(conn, identifier):
    """Unang lakang para sa "Sign in as <ngalan>" nga greeting - sulayan
    una ang username, dayon barcode - active accounts ra.

    Returns: sqlite3.Row (ngalan + photo ra) o None."""
    staff = conn.execute(
        """
        SELECT members.first_name, members.last_name, members.middle_name, members.suffix,
               staff.photo_path
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.username = ? AND staff.is_active = 1
        """,
        (identifier,),
    ).fetchone()
    if staff is None:
        staff = conn.execute(
            """
            SELECT members.first_name, members.last_name, members.middle_name, members.suffix,
                   staff.photo_path
            FROM staff
            JOIN members ON staff.member_id = members.id
            WHERE UPPER(members.barcode_id) = UPPER(?) AND staff.is_active = 1
            """,
            (identifier,),
        ).fetchone()
    return staff


def check_lockout(conn, staff):
    """Usbon ang `staff` mismo (in place) kung na-expire na ang lockout ug
    na-auto-clear na.

    Returns: None kung OK mopadayon, or (payload_dict, status) 403 - admin
    lock o naa pay nahibilin nga temporary lockout."""
    if not staff["locked_until"]:
        return None

    # Shares the lock column with an admin's manual lock; indefinite gets its own message.
    if staff["locked_until"] == INDEFINITE_LOCK_UNTIL:
        return (
            {"error": "This account has been locked by an administrator. Contact an administrator to unlock it."},
            403,
        )

    locked_until = datetime.strptime(staff["locked_until"], "%Y-%m-%d %H:%M:%S")
    if datetime.now() < locked_until:
        remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
        return ({"error": f"Account locked. Try again in {remaining} minute(s)."}, 403)

    # Clear failed_login_attempts here too, or one more wrong guess instantly re-locks the account.
    conn.execute(
        "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?", (staff["id"],)
    )
    conn.commit()
    staff["failed_login_attempts"] = 0
    staff["locked_until"] = None
    return None


def record_failed_attempt(conn, staff, attempt_kind):
    """Tawagon human mapakyas ang password/PIN check - dugangan ang
    failed_login_attempts, i-lock ang account kung na-abot na ang
    threshold.

    Returns: tuple (payload_dict, status) - 403 lockout message o 401
    "Invalid credentials"."""
    new_attempts = staff["failed_login_attempts"] + 1
    lockout_minutes = get_lockout_minutes(conn)

    if new_attempts >= get_max_failed_attempts(conn):
        lockout_until = (datetime.now() + timedelta(minutes=lockout_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE staff SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
            (new_attempts, lockout_until, staff["id"]),
        )
        conn.commit()

        log_action(
            conn=conn,
            staff_id=staff["id"],
            action="account_locked",
            target_table="staff",
            target_id=staff["id"],
            target_name=staff["username"],
            details=f"Account locked after {new_attempts} failed {attempt_kind} attempts",
        )

        return {"error": f"Too many failed attempts. Account locked for {lockout_minutes} minutes."}, 403

    conn.execute("UPDATE staff SET failed_login_attempts = ? WHERE id = ?", (new_attempts, staff["id"]))
    conn.commit()
    return {"error": "Invalid credentials"}, 401


def reset_lockout_tracking(conn, staff_id):
    """Tama ang password/PIN - i-reset ang lockout tracking, balik sa 0
    ang failed_login_attempts."""
    conn.execute("UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?", (staff_id,))
    conn.commit()


def start_session(staff_id):
    """Mag-abre ug bag-ong koneksyon (ang sa nagtawag kasagaran sirado na
    ani nga punto). Tangtangon una ang naa nang session ani nga staff_id -
    single-session-per-staff.

    Returns: tuple (token, csrf_token) - random hex strings para sa
    session_token cookie ug CSRF meta tag."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,))

        token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        absolute_hours = get_session_absolute_hours(conn)
        expires_at = (datetime.now() + timedelta(hours=absolute_hours)).strftime("%Y-%m-%d %H:%M:%S")

        conn.execute(
            "INSERT INTO sessions (token, csrf_token, staff_id, expires_at) VALUES (?, ?, ?, ?)",
            (token, csrf_token, staff_id, expires_at),
        )
        conn.commit()
        return token, csrf_token
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
