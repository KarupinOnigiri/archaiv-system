import os
import secrets
from database import get_db_connection, get_json_or_error
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import date, datetime, timedelta
from flask import (
    Blueprint,
    jsonify,
    make_response,
    request,
    render_template,
    redirect,
    url_for,
)
from utils.formatters import full_name
from utils.logger import log_action
from utils.validators import RFID_PIN_LENGTH
from services.auth_service import login_required
from services.settings_service import (
    get_session_idle_minutes,
    get_session_absolute_hours,
    get_max_failed_attempts,
    get_lockout_minutes,
    INDEFINITE_LOCK_UNTIL,
)
from extensions import limiter

auth_bp = Blueprint("auth", __name__)

# Cookies get the Secure flag (browser refuses to send them over plain
# HTTP) whenever the app isn't running in local debug mode -- same
# FLASK_DEBUG check app.py uses to decide whether the scheduler/reloader
# assume "developer's own machine over HTTP". Anything else (a kiosk
# deployment, a shared server) should be behind HTTPS, and there's no
# reason a session cookie should ever be allowed to leak over plaintext
# there.
_COOKIE_SECURE = os.environ.get("FLASK_DEBUG", "0") != "1"

# Precomputed once at import time so login() has a constant-cost hash to
# # check against when no matching account exists — keeps response timing
# # for "no such account" close to "wrong password", so timing alone
# # doesn't reveal whether a username/barcode is valid.
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def _login_response(payload, status, redirect_to=None, cookie_data=None):
    """Returns JSON if this was a JSON request (existing API behavior,
    untouched); redirects if it was a plain form POST from login.html.
    On error redirects, echoes back whatever identifier the user typed
    (username or barcode_id) so the field isn't cleared on reload —
    password is deliberately never echoed back, for the same reason
    browsers never resubmit a password into a visible field."""
    if request.is_json:
        response = make_response(jsonify(payload), status)
    else:
        if redirect_to:
            response = make_response(redirect(redirect_to))
        else:
            error_msg = payload.get("error", "Login failed")
            typed_username = (
                request.form.get("username") or request.form.get("barcode_id") or ""
            )
            if typed_username:
                response = make_response(
                    redirect(
                        url_for(
                            "pages.login_page", error=error_msg, username=typed_username
                        )
                    )
                )
            else:
                response = make_response(
                    redirect(url_for("pages.login_page", error=error_msg))
                )
    if cookie_data:
        response.set_cookie(**cookie_data)
    return response


@auth_bp.route("/login", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login():
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    username = data.get("username")
    barcode_id = data.get("barcode_id")
    password = data.get("password")

    if not password or (not username and not barcode_id):
        return jsonify({"error": "Provide username or barcode_id, plus password"}), 400

    conn = get_db_connection()

    existing_token = request.cookies.get("session_token")
    if existing_token:
        existing_session = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (existing_token,)
        ).fetchone()

        if existing_session is not None:
            now = datetime.now()
            expires_at = datetime.strptime(
                existing_session["expires_at"], "%Y-%m-%d %H:%M:%S"
            )
            last_active_at = datetime.strptime(
                existing_session["last_active_at"], "%Y-%m-%d %H:%M:%S"
            )
            session_is_live = now <= expires_at and (now - last_active_at) <= timedelta(
                minutes=get_session_idle_minutes(conn)
            )

            if session_is_live:
                current_staff = conn.execute(
                    "SELECT username FROM staff WHERE id = ?",
                    (existing_session["staff_id"],),
                ).fetchone()
                conn.close()
                return _login_response(
                    {
                        "error": "Another account is already logged in on this device. Please log out first.",
                        "current_username": (
                            current_staff["username"] if current_staff else None
                        ),
                    },
                    409,
                )
            else:
                # cookie is stale (expired/timed out) — quietly clean it up, not a real block
                conn.execute("DELETE FROM sessions WHERE token = ?", (existing_token,))
                conn.commit()

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
        # login.html's single identifier field accepts either a typed
        # username or a scanned barcode (same box, same "username" POST
        # key -- no separate field or param needed) -- if it didn't match
        # a username, try it as a barcode before giving up. Only when the
        # caller didn't *also* pass an explicit barcode_id: a JSON caller
        # that deliberately sent both shouldn't have username's failed
        # lookup silently retried against a different, unrelated value.
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
    else:
        staff = conn.execute(
            """
            SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
            FROM staff
            JOIN members ON staff.member_id = members.id
            WHERE UPPER(members.barcode_id) = UPPER(?)
            """,
            (barcode_id,),
        ).fetchone()

    if staff is None:
        conn.close()
        # run a dummy hash check so response timing doesn't reveal
        # # whether this username/barcode belongs to a real account
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        # return jsonify({"error": "Invalid credentials"}), 401
        return _login_response({"error": "Invalid credentials"}, 401)

    # Mutable from here on -- the auto-unlock branch below needs to update
    # failed_login_attempts/locked_until in place so the check just below
    # it (on this same request) sees the cleared values, not the stale
    # sqlite3.Row snapshot from the SELECT above.
    staff = dict(staff)

    # Check lockout before even touching the password
    if staff["locked_until"]:
        # The auto-lockout below (too many failed attempts) and an admin's
        # manual lock (routes/staff.py: lock_staff) both just write to this
        # same column -- from here they're indistinguishable except for the
        # sentinel value an indefinite manual lock uses, which gets its own
        # message instead of a "try again in ~4 million minutes" one.
        if staff["locked_until"] == INDEFINITE_LOCK_UNTIL:
            conn.close()
            return _login_response(
                {"error": "This account has been locked by an administrator. Contact an administrator to unlock it."},
                403,
            )
        locked_until = datetime.strptime(staff["locked_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < locked_until:
            conn.close()
            remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
            return _login_response(
                {
                    "error": f"Account locked. Try again in {remaining} minute(s)."
                },
                403,
            )
        # Lockout window has elapsed. failed_login_attempts is only ever
        # reset to 0 on a *successful* login further down -- without
        # clearing it here too, it's still sitting at (or above)
        # max_failed_attempts, so a single further wrong guess right after
        # expiry would immediately re-trip the "too many attempts" branch
        # below and re-lock the account for a full fresh window. That
        # looked indistinguishable from "the lockout never actually
        # expired" even though the time-based check above was working
        # correctly the whole time.
        conn.execute(
            "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
            (staff["id"],),
        )
        conn.commit()
        staff["failed_login_attempts"] = 0
        staff["locked_until"] = None

    if not check_password_hash(staff["password_hash"], password):
        new_attempts = staff["failed_login_attempts"] + 1
        lockout_minutes = get_lockout_minutes(conn)

        if new_attempts >= get_max_failed_attempts(conn):
            lockout_until = (
                datetime.now() + timedelta(minutes=lockout_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE staff SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                (new_attempts, lockout_until, staff["id"]),
            )
            conn.commit()
            conn.close()

            log_action(
                staff_id=staff["id"],
                action="account_locked",
                target_table="staff",
                target_id=staff["id"],
                target_name=staff["username"],
                details=f"Account locked after {new_attempts} failed login attempts",
            )

            return _login_response(
                {
                    "error": f"Too many failed attempts. Account locked for {lockout_minutes} minutes."
                },
                403,
            )

        conn.execute(
            "UPDATE staff SET failed_login_attempts = ? WHERE id = ?",
            (new_attempts, staff["id"]),
        )
        conn.commit()
        conn.close()
        # return jsonify({"error": "Invalid credentials"}), 401
        return _login_response({"error": "Invalid credentials"}, 401)

    # Correct password — reset lockout tracking
    conn.execute(
        "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (staff["id"],),
    )
    conn.commit()
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    if staff["role"] == "tool_keeper":
        if not staff["assigned_start"] or not staff["assigned_end"]:
            return _login_response(
                {
                    "error": "No assignment period set for this account. Contact an admin."
                },
                403,
            )
        today = date.today().isoformat()
        if today < staff["assigned_start"]:
            return _login_response(
                {"error": "Tool keeper assignment has not started yet"}, 403
            )
        if today > staff["assigned_end"]:
            return _login_response({"error": "Tool keeper assignment has expired"}, 403)
    session_conn = get_db_connection()

    try:
        session_conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff["id"],))

        token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        # Captured once here and reused below for the cookie's max_age --
        # session_conn is closed (see finally below) by the time the
        # cookie gets set, so it can't be looked up again there.
        absolute_hours = get_session_absolute_hours(session_conn)
        expires_at = (
            datetime.now() + timedelta(hours=absolute_hours)
        ).strftime("%Y-%m-%d %H:%M:%S")

        session_conn.execute(
            "INSERT INTO sessions (token, csrf_token, staff_id, expires_at) VALUES (?, ?, ?, ?)",
            (token, csrf_token, staff["id"], expires_at),
        )
        session_conn.commit()
    except Exception:
        session_conn.rollback()
        raise
    finally:
        session_conn.close()

    # Symmetric with login_rfid's "logged_in_rfid" audit entry below --
    # every successful sign-in (password/barcode here, RFID+PIN there)
    # should leave a trace, for every role, not just the RFID admin path.
    log_action(
        staff_id=staff["id"],
        action="logged_in",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Logged in via username/barcode + password: {staff['username']}",
    )

    response = _login_response(
        {
            "message": "Login successful",
            "staff_id": staff["id"],
            "name": full_name(
                staff["first_name"],
                staff["last_name"],
                staff["middle_name"],
                staff["suffix"],
            ),
            "role": staff["role"],
            "csrf_token": csrf_token,
        },
        200,
        # The temporary setup admin (services.seed_service) lands on the
        # setup wizard instead of the normal dashboard -- see
        # pages.setup_page's docstring for why.
        redirect_to=url_for("pages.setup_page") if staff["is_system_seeded"] else url_for("home"),
    )
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="Lax",
        secure=_COOKIE_SECURE,
        max_age=absolute_hours * 3600,
    )
    return response


@auth_bp.route("/login-rfid", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login_rfid():
    """RFID-card login -- admin-only by design (see the docstring
    discussion that led here): a keycard alone is "something you have"
    with no second factor, and cloning/losing a card would otherwise
    hand over the single most privileged role in the system with
    nothing to stop it. Pairs the card (staff.rfid_id) with a short PIN
    (staff.rfid_pin_hash, hashed the same way a password is) instead --
    still much faster to enter at a reader than a full password, but a
    stolen/cloned card alone isn't enough on its own.

    Shares its lockout bookkeeping (failed_login_attempts/locked_until)
    with the regular password login above rather than tracking it
    separately -- simpler, and there's no reason repeated bad PIN
    attempts should be lawed differently than repeated bad passwords for
    the same account."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    rfid_id = data.get("rfid_id")
    pin = data.get("pin")

    if not rfid_id or not pin:
        return jsonify({"error": "Provide rfid_id and pin"}), 400

    # PINs are always exactly RFID_PIN_LENGTH digits (see
    # utils/validators.py:validate_rfid_pin, used when a PIN is set).
    # Malformed input here can't be a legitimate near-miss guess of a real
    # PIN, so it's safe to reject before even touching the DB -- same
    # generic message as a wrong PIN so it doesn't leak the format rule to
    # anyone probing the endpoint.
    if not pin.isdigit() or len(pin) != RFID_PIN_LENGTH:
        return jsonify({"error": "Invalid credentials"}), 401

    conn = get_db_connection()

    existing_token = request.cookies.get("session_token")
    if existing_token:
        existing_session = conn.execute(
            "SELECT * FROM sessions WHERE token = ?", (existing_token,)
        ).fetchone()

        if existing_session is not None:
            now = datetime.now()
            expires_at = datetime.strptime(
                existing_session["expires_at"], "%Y-%m-%d %H:%M:%S"
            )
            last_active_at = datetime.strptime(
                existing_session["last_active_at"], "%Y-%m-%d %H:%M:%S"
            )
            session_is_live = now <= expires_at and (now - last_active_at) <= timedelta(
                minutes=get_session_idle_minutes(conn)
            )

            if session_is_live:
                current_staff = conn.execute(
                    "SELECT username FROM staff WHERE id = ?",
                    (existing_session["staff_id"],),
                ).fetchone()
                conn.close()
                return _login_response(
                    {
                        "error": "Another account is already logged in on this device. Please log out first.",
                        "current_username": (
                            current_staff["username"] if current_staff else None
                        ),
                    },
                    409,
                )
            else:
                conn.execute("DELETE FROM sessions WHERE token = ?", (existing_token,))
                conn.commit()

    staff = conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(staff.rfid_id) = UPPER(?) AND staff.role = 'admin'
        """,
        (rfid_id,),
    ).fetchone()

    if staff is None or not staff["rfid_pin_hash"]:
        conn.close()
        # Same timing-safety reasoning as the password login above --
        # constant-cost dummy check whether the card doesn't exist, isn't
        # an admin's, or has no PIN set yet, so response timing alone
        # can't distinguish those cases from a wrong PIN.
        check_password_hash(_DUMMY_PASSWORD_HASH, pin)
        return _login_response({"error": "Invalid credentials"}, 401)

    # See the mirrored comment in login() above -- mutable so the
    # auto-unlock branch below can update failed_login_attempts/
    # locked_until in place for the rest of this same request.
    staff = dict(staff)

    if staff["locked_until"]:
        if staff["locked_until"] == INDEFINITE_LOCK_UNTIL:
            conn.close()
            return _login_response(
                {"error": "This account has been locked by an administrator. Contact an administrator to unlock it."},
                403,
            )
        locked_until = datetime.strptime(staff["locked_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < locked_until:
            conn.close()
            remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
            return _login_response(
                {"error": f"Account locked. Try again in {remaining} minute(s)."},
                403,
            )
        # Lockout window elapsed -- see login()'s identical fix above for
        # why this has to happen here too, not just on a later success.
        conn.execute(
            "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
            (staff["id"],),
        )
        conn.commit()
        staff["failed_login_attempts"] = 0
        staff["locked_until"] = None

    if not check_password_hash(staff["rfid_pin_hash"], pin):
        new_attempts = staff["failed_login_attempts"] + 1
        lockout_minutes = get_lockout_minutes(conn)

        if new_attempts >= get_max_failed_attempts(conn):
            lockout_until = (
                datetime.now() + timedelta(minutes=lockout_minutes)
            ).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE staff SET failed_login_attempts = ?, locked_until = ? WHERE id = ?",
                (new_attempts, lockout_until, staff["id"]),
            )
            conn.commit()
            conn.close()

            log_action(
                staff_id=staff["id"],
                action="account_locked",
                target_table="staff",
                target_id=staff["id"],
                target_name=staff["username"],
                details=f"Account locked after {new_attempts} failed RFID PIN attempts",
            )

            return _login_response(
                {"error": f"Too many failed attempts. Account locked for {lockout_minutes} minutes."},
                403,
            )

        conn.execute(
            "UPDATE staff SET failed_login_attempts = ? WHERE id = ?",
            (new_attempts, staff["id"]),
        )
        conn.commit()
        conn.close()
        return _login_response({"error": "Invalid credentials"}, 401)

    conn.execute(
        "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (staff["id"],),
    )
    conn.commit()
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    session_conn = get_db_connection()
    try:
        session_conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff["id"],))

        token = secrets.token_hex(32)
        csrf_token = secrets.token_hex(32)
        absolute_hours = get_session_absolute_hours(session_conn)
        expires_at = (
            datetime.now() + timedelta(hours=absolute_hours)
        ).strftime("%Y-%m-%d %H:%M:%S")

        session_conn.execute(
            "INSERT INTO sessions (token, csrf_token, staff_id, expires_at) VALUES (?, ?, ?, ?)",
            (token, csrf_token, staff["id"], expires_at),
        )
        session_conn.commit()
    except Exception:
        session_conn.rollback()
        raise
    finally:
        session_conn.close()

    # log_action for a successful RFID login, distinctly tagged rather
    # than reusing the same action name a password login would -- see
    # the design discussion this feature came out of: RFID-originated
    # sessions should stay visible/distinguishable in the audit trail,
    # not blend in with ordinary password logins.
    log_action(
        staff_id=staff["id"],
        action="logged_in_rfid",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Logged in via RFID card + PIN: {staff['username']}",
    )

    response = _login_response(
        {
            "message": "Login successful",
            "staff_id": staff["id"],
            "name": full_name(
                staff["first_name"],
                staff["last_name"],
                staff["middle_name"],
                staff["suffix"],
            ),
            "role": staff["role"],
            "csrf_token": csrf_token,
        },
        200,
        redirect_to=url_for("home"),
    )
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="Lax",
        secure=_COOKIE_SECURE,
        max_age=absolute_hours * 3600,
    )
    return response


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():
    token = request.cookies.get("session_token")

    if token:
        conn = get_db_connection()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()

    response = make_response(jsonify({"message": "Logged out successfully"}))
    response.headers["HX-Redirect"] = "/login"
    response.set_cookie(
        "session_token",
        "",
        httponly=True,
        samesite="Lax",
        secure=_COOKIE_SECURE,
        max_age=0,
    )
    return response
