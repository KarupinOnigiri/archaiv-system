"""Login/session-establishment logic for routes/auth.py (username+password,
RFID+PIN, and logout). Every function here takes an already-open `conn` (except
start_session, which needs a fresh one since the caller's conn is often already
closed by the time a session is minted) and returns plain data - never a Flask
response - so routes/auth.py keeps full control of how errors get echoed back
(JSON vs. redirect-with-query-string), which is security/UX-sensitive here.

Convention: `(data, error)` where `error` is `None` on success or a plain
`(payload_dict, status)` tuple on failure - not jsonify'd, since the route needs
the raw dict either way (JSON body or to extract `error` for a redirect).

(Bisaya: Kini nga file ang naghandle sa login/session - username+password,
RFID+PIN, ug logout - para sa routes/auth.py. Matag function dinhi mo-dawat
ug naa nay bukas nga `conn` - gawas sa start_session, nga kinahanglan mag-
abre ug bag-o kay ang conn sa nagtawag kasagaran sirado na - ug mobalik ug
plain data, DILI Flask response, para permanente nga makontrol sa
routes/auth.py kung unsaon pagpakita ang error - JSON o redirect-with-
query-string - importante ni sa security/UX.

Convention: `(data, error)` diin ang `error` None kung malampuson, o usa ka
plain `(payload_dict, status)` nga tuple kung napakyas.)
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
    """Single-session-per-device policy: blocks login if a live session already
    exists on this browser, belonging to anyone. Cleans up a stale (expired/timed
    out) token instead of blocking.
    (Bisaya: Usa-ka-session-ra-matag-device nga patakaran - i-block ang
    login kung naay buhi/live nga session pa sa niining browser, bisan
    kang kinsa. Kung stale na - na-expire/timeout - hinlo-an ra kini, dili
    ni-block.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        token: the session_token cookie value from the request, or None.
        (Bisaya: value sa session_token cookie gikan sa request, o None.)

    Returns:
        tuple (blocked, payload_or_None) - `blocked` True means refuse the
        login attempt; `payload` (when blocked) explains whose session is
        active.
        (Bisaya: usa ka tuple (blocked, payload_or_None) - True ang
        `blocked` nagpasabot nga i-refuse ang login; ang `payload` - kung
        blocked - nagsulti kang kinsa nga session ang active.)
    """
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
    """Looks up by username first, retrying as a barcode if that misses (unless
    barcode_id was already passed explicitly), or by barcode_id directly.
    (Bisaya: Pangitaon una base sa username, kung wala makit-i, sulayan
    isip barcode - gawas kung gipasa na gyud ang barcode_id direkta - o
    barcode_id direkta.

    Args: conn, username (o None), barcode_id (o None).
    Returns: sqlite3.Row (staff + member info) o None kung wala makit-i.)"""
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
    """Finds an active admin by their rfid_id, for the (currently unused,
    kept for a possible future re-activation) RFID+PIN login flow.
    (Bisaya: Pangitaon ang admin base sa iyang rfid_id - para sa RFID+PIN
    login flow, nga karon wala pa gigamit apan gibutang para sa posible
    nga umaabot nga paggamit pag-usab.

    Args: conn, rfid_id.
    Returns: sqlite3.Row o None.)"""
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


def find_admin_for_rfid_lookup(conn, rfid_id):
    """Identifies which admin a tapped card belongs to before the PIN, same generic response as login_rfid.
    (Bisaya: I-identify kang kinsang admin ang gi-tap nga card, sa dili pa
    hangyoon ang PIN - pareho ka generic nga response sa login_rfid.

    Args: conn, rfid_id.
    Returns: sqlite3.Row (limited columns) o None.)"""
    return conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.rfid_pin_hash, staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE (UPPER(staff.rfid_id) = UPPER(?) OR UPPER(staff.rfid_id) LIKE '%' || ?) AND staff.role = 'admin'
        """,
        (rfid_id, decimal_scan_hex_suffix(rfid_id)),
    ).fetchone()


def find_staff_for_barcode_pin_login(conn, barcode_id):
    """Same shape as find_admin_for_rfid_login, but by barcode_id (which
    lives on the linked members row, not staff) and open to every role --
    login_barcode_pin() is the general opt-in flow this app now actually
    uses at the login page; find_admin_for_rfid_login above stays reserved
    for a possible future RFID+PIN re-activation, untouched.
    (Bisaya: Pareho ka porma sa find_admin_for_rfid_login, apan base sa
    barcode_id - naa ni sa members row, dili sa staff - ug bukas sa TANANG
    role, dili admin ra. Kini ang tinuod nga gigamit karon sa login page.

    Args: conn, barcode_id.
    Returns: sqlite3.Row o None.)"""
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
    """Identifies whose barcode was scanned before the PIN, same generic
    response as login_barcode_pin - mirrors find_admin_for_rfid_lookup.
    (Bisaya: I-identify kang kinsang barcode ang gi-scan, sa dili pa
    hangyoon ang PIN - pareho ka generic nga response.

    Args: conn, barcode_id.
    Returns: sqlite3.Row (limited columns) o None.)"""
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
    """Step-1 check for the "Sign in as <name>" greeting; tries username then barcode, active accounts only.
    (Bisaya: Unang lakang para makita ang "Sign in as <ngalan>" nga
    pangumusta - sulayan una ang username, dayon barcode - active nga
    account ra.

    Args: conn, identifier (username o barcode text nga gi-type/scan).
    Returns: sqlite3.Row (ngalan + photo ra) o None.)"""
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
    Returns None if it's OK to proceed, or a (payload, status) error otherwise.
    (Bisaya: Usbon/mutate ang `staff` mismo kung ang lockout na-expire na ug
    na-auto-clear. None ang ibalik kung OK na mopadayon, o usa ka (payload,
    status) nga error kung dili pa.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff: the staff row being logged into (has locked_until,
            failed_login_attempts) - modified in place when a lockout
            auto-clears.
        (Bisaya: ang staff row nga gina-login-an - naa ni'y locked_until,
        failed_login_attempts - mausab mismo kung mag-auto-clear ang
        lockout.)

    Returns:
        None when OK to proceed, or (payload_dict, status) - 403 either
        for an admin-imposed indefinite lock, or a still-counting-down
        timed lockout.
        (Bisaya: None kung OK na mopadayon, o (payload_dict, status) - 403
        - kung na-lock permanente sa admin, o naa pay nahibilin nga oras
        sa temporary nga lockout.)
    """
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
    locking the account if the threshold is hit. Always returns a (payload, status) error.
    (Bisaya: Tawagon human mapakyas ang password/PIN check. Dugangan ang
    failed_login_attempts, i-lock ang account kung na-abot na ang
    threshold/limit. Kanunay mobalik ug (payload, status) nga error - kay
    napakyas man gyud ang attempt.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff: the staff row that just failed to authenticate.
        (Bisaya: ang staff row nga napakyas mo-login.)
        attempt_kind: label for the audit log entry, e.g. "login" or
            "PIN".
        (Bisaya: label para sa audit log, e.g. "login" o "PIN".)

    Returns:
        tuple (payload_dict, status) - 403 with a lockout message when the
        threshold was just hit, 401 "Invalid credentials" otherwise.
        (Bisaya: usa ka tuple (payload_dict, status) - 403 nga naay
        lockout message kung na-abot ang threshold karon, 401 "Invalid
        credentials" kung dili pa.)
    """
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
    """Correct password/PIN - reset lockout tracking.
    (Bisaya: Tama ang password/PIN - i-reset ang lockout tracking - balik
    sa 0 ang failed_login_attempts.

    Args: conn, staff_id.
    Returns: None.)"""
    conn.execute("UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?", (staff_id,))
    conn.commit()


def start_session(staff_id):
    """Opens its own connection (the caller's is often already closed by this point).
    Deletes any existing session for this staff_id first - single-session-
    per-staff, mirroring check_existing_session_conflict's per-device rule.
    (Bisaya: Mag-abre ug bag-ong koneksyon - ang naa sa nagtawag kasagaran
    sirado na sa niining punto. Tangtangon una ang naa nang session ani
    nga staff_id - usa-ka-session-ra-matag-staff, sunod sa parehas nga
    lohika sa check_existing_session_conflict's per-device nga
    patakaran.)

    Args:
        staff_id: id of the staff member to start a session for.
        (Bisaya: id sa staff nga hatagan ug bag-ong session.)

    Returns:
        tuple (token, csrf_token) - both random hex strings; `token` goes
        in the session_token cookie, `csrf_token` is embedded in the page
        (meta tag) for state-changing requests to echo back.
        (Bisaya: usa ka tuple (token, csrf_token) - pareho random nga hex
        string; ang `token` isulod sa session_token cookie, ang
        `csrf_token` isulod sa page - meta tag - ibalik sa mga request nga
        mag-usab ug data.)
    """
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
