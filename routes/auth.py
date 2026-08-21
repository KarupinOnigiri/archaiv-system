import os
from database import get_db_connection, get_json_or_error
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import date
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
from services import login_service
from extensions import limiter

auth_bp = Blueprint("auth", __name__)

# COOKIE_SECURE is its own env var, separate from FLASK_DEBUG -- see .env.example / DEPLOYMENT.md.
_cookie_secure_env = os.environ.get("COOKIE_SECURE")
if _cookie_secure_env is not None:
    _COOKIE_SECURE = _cookie_secure_env == "1"
else:
    _COOKIE_SECURE = os.environ.get("FLASK_DEBUG", "0") != "1"

# Constant-cost dummy hash, so "no such account" and "wrong password" take the same time.
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def _login_response(payload, status, redirect_to=None, cookie_data=None):
    """JSON for JSON requests, redirect for form POSTs; echoes username/barcode back on error, never password."""
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

    blocked, payload = login_service.check_existing_session_conflict(conn, request.cookies.get("session_token"))
    if blocked:
        conn.close()
        return _login_response(payload, 409)

    staff = login_service.find_staff_for_login(conn, username, barcode_id)

    if staff is None:
        conn.close()
        # dummy hash check so timing doesn't reveal whether the account exists
        check_password_hash(_DUMMY_PASSWORD_HASH, password)
        return _login_response({"error": "Invalid credentials"}, 401)

    # dict so auto-unlock below can mutate values and the rest of the request sees them.
    staff = dict(staff)

    # Check lockout before even touching the password
    lockout_error = login_service.check_lockout(conn, staff)
    if lockout_error:
        conn.close()
        payload, status = lockout_error
        return _login_response(payload, status)

    if not check_password_hash(staff["password_hash"], password):
        payload, status = login_service.record_failed_attempt(conn, staff, "login")
        conn.close()
        return _login_response(payload, status)

    # Correct password — reset lockout tracking
    login_service.reset_lockout_tracking(conn, staff["id"])
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    if staff["role"] == "tool_keeper":
        if not staff["assigned_start"] or not staff["assigned_end"]:
            return _login_response(
                {"error": "No assignment period set for this account. Contact an admin."}, 403
            )
        today = date.today().isoformat()
        if today < staff["assigned_start"]:
            return _login_response({"error": "Tool keeper assignment has not started yet"}, 403)
        if today > staff["assigned_end"]:
            return _login_response({"error": "Tool keeper assignment has expired"}, 403)

    token, csrf_token = login_service.start_session(staff["id"])

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
                staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]
            ),
            "role": staff["role"],
            "csrf_token": csrf_token,
        },
        200,
        # temp setup admin (services.seed_service) goes to the setup wizard, not the dashboard
        redirect_to=url_for("pages.setup_page") if staff["is_system_seeded"] else url_for("home"),
    )
    # No max_age on purpose -- a session cookie, so the next shared-terminal user isn't auto-logged-in.
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
    """Step-1 check for the "Sign in as <name>" greeting; generic 404 so accounts can't be enumerated."""
    data, error = get_json_or_error()
    if error:
        return error

    identifier = (data.get("identifier") or "").strip()
    if not identifier:
        return jsonify({"error": "Provide identifier"}), 400

    conn = get_db_connection()
    try:
        staff = login_service.find_staff_for_username_lookup(conn, identifier)
    finally:
        conn.close()

    if staff is None:
        return jsonify({"error": "We couldn't find an account with that username or barcode ID."}), 404

    return jsonify(
        {
            "name": full_name(staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]),
            "photo_path": staff["photo_path"],
        }
    )


@auth_bp.route("/rfid-lookup", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def rfid_lookup():
    """Identifies which admin a tapped card belongs to before the PIN, same generic response as login_rfid."""
    data, error = get_json_or_error()
    if error:
        return error

    rfid_id = data.get("rfid_id")
    if not rfid_id:
        return jsonify({"error": "Provide rfid_id"}), 400

    conn = get_db_connection()
    try:
        staff = login_service.find_admin_for_rfid_lookup(conn, rfid_id)
    finally:
        conn.close()

    if staff is None or not staff["rfid_pin_hash"] or not staff["is_active"]:
        return jsonify({"error": "Card not recognized"}), 404

    return jsonify(
        {
            "name": full_name(staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]),
            "photo_path": staff["photo_path"],
        }
    )


@auth_bp.route("/login-rfid", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login_rfid():
    """RFID login, admin-only; paired with a hashed PIN so a stolen card alone isn't enough."""
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

    # Rejects a malformed PIN before touching the DB, with the same generic error either way.
    if not pin.isdigit() or len(pin) != RFID_PIN_LENGTH:
        return jsonify({"error": "Invalid credentials"}), 401

    conn = get_db_connection()

    blocked, payload = login_service.check_existing_session_conflict(conn, request.cookies.get("session_token"))
    if blocked:
        conn.close()
        return _login_response(payload, 409)

    staff = login_service.find_admin_for_rfid_login(conn, rfid_id)

    if staff is None or not staff["rfid_pin_hash"]:
        conn.close()
        # same timing-safety trick as login() -- constant-cost check either way
        check_password_hash(_DUMMY_PASSWORD_HASH, pin)
        return _login_response({"error": "Invalid credentials"}, 401)

    # mutable so the auto-unlock branch below can update in place, same as login() above
    staff = dict(staff)

    lockout_error = login_service.check_lockout(conn, staff)
    if lockout_error:
        conn.close()
        payload, status = lockout_error
        return _login_response(payload, status)

    if not check_password_hash(staff["rfid_pin_hash"], pin):
        payload, status = login_service.record_failed_attempt(conn, staff, "RFID PIN")
        conn.close()
        return _login_response(payload, status)

    login_service.reset_lockout_tracking(conn, staff["id"])
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    token, csrf_token = login_service.start_session(staff["id"])

    # Distinct action name so RFID logins stay visible, not blended with password logins.
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
                staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]
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
