"""Login/session-establishment logic for routes/auth.py (username+password,
RFID+PIN, and logout). Every function here takes an already-open `conn` (except
start_session, which needs a fresh one since the caller's conn is often already
closed by the time a session is minted) and returns plain data -- never a Flask
response -- so routes/auth.py keeps full control of how errors get echoed back
(JSON vs. redirect-with-query-string), which is security/UX-sensitive here.

Convention: `(data, error)` where `error` is `None` on success or a plain
`(payload_dict, status)` tuple on failure -- not jsonify'd, since the route needs
the raw dict either way (JSON body or to extract `error` for a redirect).
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


def check_existing_session_conflict(conn, token):
    """Single-session-per-device policy: blocks login if a live session already
    exists on this browser, belonging to anyone. Cleans up a stale (expired/timed
    out) token instead of blocking. Returns (blocked: bool, payload_or_None)."""
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

    # stale cookie (expired/timed out) -- just clean it up, not a real block
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    return False, None


def find_staff_for_login(conn, username, barcode_id):
    """Looks up by username first, retrying as a barcode if that misses (unless
    barcode_id was already passed explicitly), or by barcode_id directly."""
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
    return conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(staff.rfid_id) = UPPER(?) AND staff.role = 'admin'
        """,
        (rfid_id,),
    ).fetchone()


def find_admin_for_rfid_lookup(conn, rfid_id):
    """Identifies which admin a tapped card belongs to before the PIN, same generic response as login_rfid."""
    return conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.rfid_pin_hash, staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(staff.rfid_id) = UPPER(?) AND staff.role = 'admin'
        """,
        (rfid_id,),
    ).fetchone()


def find_staff_for_username_lookup(conn, identifier):
    """Step-1 check for the "Sign in as <name>" greeting; tries username then barcode, active accounts only."""
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
    """Mutates `staff` in place if a lockout has expired and gets auto-cleared.
    Returns None if it's OK to proceed, or a (payload, status) error otherwise."""
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
    """Call after a failed password/PIN check. Increments failed_login_attempts,
    locking the account if the threshold is hit. Always returns a (payload, status) error."""
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
    """Correct password/PIN -- reset lockout tracking."""
    conn.execute("UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?", (staff_id,))
    conn.commit()


def start_session(staff_id):
    """Opens its own connection (the caller's is often already closed by this point).
    Returns (token, csrf_token)."""
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
