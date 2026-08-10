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

# Cookies get Secure unless we're in local FLASK_DEBUG mode (same check app.py uses) -- anything
# else is a kiosk/shared server and should be HTTPS-only.
_COOKIE_SECURE = os.environ.get("FLASK_DEBUG", "0") != "1"

# Constant-cost hash to check against when no account matches, so "no such account" and
# "wrong password" take the same time and timing can't leak which usernames exist.
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def _login_response(payload, status, redirect_to=None, cookie_data=None):
    """JSON response for JSON requests, redirect for plain form POSTs from login.html.
    On error redirects, echoes back the typed username/barcode (never the password) so the
    field survives a page reload."""
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
                # stale cookie (expired/timed out) -- just clean it up, not a real block
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
        # login.html's identifier field can hold either a username or scanned barcode, so retry
        # as a barcode if the username lookup misses -- but only when barcode_id wasn't also
        # explicitly passed (that'd mean the caller sent two distinct values on purpose).
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
        # dummy hash check so timing doesn't reveal whether the account exists
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        return _login_response({"error": "Invalid credentials"}, 401)

    # dict so the auto-unlock branch below can mutate failed_login_attempts/locked_until
    # in place and have the rest of this request see the updated values
    staff = dict(staff)

    # Check lockout before even touching the password
    if staff["locked_until"]:
        # Auto-lockout and an admin's manual lock (routes/staff.py: lock_staff) share this same
        # column; the indefinite-lock sentinel gets its own message instead of "try again in ~4
        # million minutes".
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
        # Lockout expired but failed_login_attempts only resets on a successful login -- clear it
        # here too, or the next wrong guess immediately re-trips the "too many attempts" branch
        # and re-locks the account, making it look like the lockout never actually expired.
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
        # grab this now -- session_conn is closed by the time the cookie gets set below
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

    # mirrors login_rfid's "logged_in_rfid" entry -- every successful sign-in gets logged
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
        # temp setup admin (services.seed_service) goes to the setup wizard, not the dashboard
        redirect_to=url_for("pages.setup_page") if staff["is_system_seeded"] else url_for("home"),
    )
    # No max_age on purpose -- makes it a session cookie so the next person on this shared
    # terminal doesn't inherit the previous login. Doesn't weaken security: expires_at and the
    # idle timeout are re-checked server-side on every request regardless.
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="Lax",
        secure=_COOKIE_SECURE,
    )
    return response


@auth_bp.route("/username-lookup", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def username_lookup():
    """Gmail-style step-1 check: does this username/barcode belong to a real, active account?
    Lets the login page greet "Sign in as <name>" with a photo before asking for a password.
    Returns the same generic 404 for unknown/inactive/ineligible accounts so it can't be used
    to enumerate which accounts exist -- rate limited for the same reason."""
    data, error = get_json_or_error()
    if error:
        return error

    identifier = (data.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"error": "Provide identifier"}), 400

    conn = get_db_connection()
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
    conn.close()

    if staff is None:
        return jsonify({"error": "We couldn't find an account with that username or barcode ID."}), 404

    return jsonify(
        {
            "name": full_name(
                staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]
            ),
            "photo_path": staff["photo_path"],
        }
    )


@auth_bp.route("/rfid-lookup", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def rfid_lookup():
    """Looks up which admin a scanned card belongs to before the PIN is entered, so the login
    page can greet "Sign in as <name>" the moment a card is tapped. Safe to do PIN-less since
    the card ID isn't a secret (any reader can read it). Same eligibility gate and generic
    response as login_rfid, so it can't be used to fish for which cards exist."""
    data, error = get_json_or_error()
    if error:
        return error

    rfid_id = data.get("rfid_id")
    if not rfid_id:
        return jsonify({"error": "Provide rfid_id"}), 400

    conn = get_db_connection()
    staff = conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.rfid_pin_hash, staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE UPPER(staff.rfid_id) = UPPER(?) AND staff.role = 'admin'
        """,
        (rfid_id,),
    ).fetchone()
    conn.close()

    if staff is None or not staff["rfid_pin_hash"] or not staff["is_active"]:
        return jsonify({"error": "Card not recognized"}), 404

    return jsonify(
        {
            "name": full_name(
                staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]
            ),
            "photo_path": staff["photo_path"],
        }
    )


@auth_bp.route("/login-rfid", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login_rfid():
    """RFID-card login, admin-only: a card alone is "something you have" with no second
    factor, so it's paired with a short PIN (hashed like a password) to stop a stolen/cloned
    card from being enough on its own. Shares lockout bookkeeping with the password login
    above rather than tracking it separately."""
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

    # PINs are always RFID_PIN_LENGTH digits (see validate_rfid_pin) -- reject malformed input
    # before touching the DB, same generic error so it doesn't leak the format rule.
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
        # same timing-safety trick as login() -- constant-cost check either way
        check_password_hash(_DUMMY_PASSWORD_HASH, pin)
        return _login_response({"error": "Invalid credentials"}, 401)

    # mutable so the auto-unlock branch below can update in place, same as login() above
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
        # lockout elapsed -- same reset-here-too fix as login() above
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

    # distinct action name so RFID logins stay visible in the audit trail, not blended with password logins
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
    # session cookie on purpose (no max_age) -- see login() above for why
    response.set_cookie(
        "session_token",
        token,
        httponly=True,
        samesite="Lax",
        secure=_COOKIE_SECURE,
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
