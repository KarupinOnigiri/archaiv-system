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
from services.settings_service import PERMANENT_ASSIGNED_END
from extensions import limiter

"""Thin HTTP layer para sa login/logout ug session endpoints - ang actual
verification (lockout, timing-safety, session creation) naa sa
services/login_service.py, dinhi ra ang request parsing ug response shaping."""

auth_bp = Blueprint("auth", __name__)

# COOKIE_SECURE is its own env var, separate from FLASK_DEBUG - see .env.example / DEPLOYMENT.md.
_cookie_secure_env = os.environ.get("COOKIE_SECURE")
if _cookie_secure_env is not None:
    _COOKIE_SECURE = _cookie_secure_env == "1"
else:
    _COOKIE_SECURE = os.environ.get("FLASK_DEBUG", "0") != "1"

# Constant-cost dummy hash, so "no such account" and "wrong password" take the same time.
_DUMMY_PASSWORD_HASH = generate_password_hash("dummy-password-for-timing-safety")


def _check_assignment_window(staff):
    """Mag-block ug login kung ang account's assignment window dili pa/dili
    na active. Required jud ni for Tool Keeper - blangko nga
    assigned_start/assigned_end nagpasabot "wala pa gyud gi-assign," dili
    "walay restriction," so dili gyud sila makalogin hangtod naay admin
    mo-set. Optional para sa In Charge - blangko or PERMANENT_ASSIGNED_END
    nga assigned_end means walay restriction, pero kung naay genuine end
    date na-set (per staff_service.compute_usage_status), i-enforce gyud
    parehas sa Tool Keeper.

    Exempt ang admin. Gitawag ni sa login() ug login_barcode_pin() ra -
    dili sa login_rfid() kay admin-only man na siya.

    Returns None kung pwede na mo-proceed ang login, or (payload_dict,
    status) tuple kung naay error - raw dict pa, dili jsonify'd, para
    ma-reuse sa JSON response or redirect."""
    role = staff["role"]
    if role == "admin":
        return None

    today = date.today().isoformat()

    if role == "tool_keeper":
        if not staff["assigned_start"] or not staff["assigned_end"]:
            return {"error": "No assignment period set for this account. Contact an admin."}, 403
        if today < staff["assigned_start"]:
            return {"error": "Tool keeper assignment has not started yet"}, 403
        if today > staff["assigned_end"]:
            return {"error": "Tool keeper assignment has expired"}, 403
        return None

    # in_charge: no assigned_end (or the permanent sentinel) means no
    # restriction was ever set for this account - only enforce the window
    # once a real end date is actually on file.
    if staff["assigned_end"] and staff["assigned_end"] != PERMANENT_ASSIGNED_END:
        if staff["assigned_start"] and today < staff["assigned_start"]:
            return {"error": "Assignment has not started yet"}, 403
        if today > staff["assigned_end"]:
            return {"error": "Assignment has expired"}, 403
    return None


def _safe_next_url():
    """Asa i-redirect after login kung gikan sila sa page nga na-bounce
    (session expired mid-use, login_required nag-carry ug ?next=<path>).
    Same-site relative path ra ang gi-honor - kung absolute/external ang
    `next`, open-redirect risk na na, so mo-fall through na lang sa normal
    post-login destination."""
    next_url = request.form.get("next") or request.args.get("next") or ""
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return None


def _login_response(payload, status, redirect_to=None, cookie_data=None):
    """JSON kung JSON request, redirect kung form POST; i-echo balik ang
    username/barcode pag naay error, pero dili gyud ang password."""
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
    """POST - username/barcode + password login. Delegates ang lookup,
    lockout, password check, ug session creation sa services/login_service.py;
    kini na route ra ang nag-assemble sa response ug session cookie."""
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

    # Correct password - reset lockout tracking
    login_service.reset_lockout_tracking(conn, staff["id"])
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    assignment_error = _check_assignment_window(staff)
    if assignment_error:
        payload, status = assignment_error
        return _login_response(payload, status)

    token, csrf_token = login_service.start_session(staff["id"])

    # mirrors login_rfid's "logged_in_rfid" entry - every successful sign-in gets logged
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
        # temp setup admin (services.seed_service) goes to the setup wizard, not the dashboard --
        # takes priority over `next` even if one was carried through, since a not-yet-set-up
        # system doesn't have whatever page `next` pointed at working yet anyway.
        redirect_to=(
            url_for("pages.setup_page")
            if staff["is_system_seeded"]
            else (_safe_next_url() or url_for("home"))
        ),
    )
    # No max_age on purpose - a session cookie, so the next shared-terminal user isn't auto-logged-in.
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
    """POST - step-1 check para sa "Sign in as <name>" greeting; generic 404
    ang response para dili ma-enumerate ang mga existing accounts."""
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


@auth_bp.route("/barcode-pin-lookup", methods=["POST"])
@limiter.limit("20 per minute; 100 per hour")
def barcode_pin_lookup():
    """POST - i-identify kinsa ang tag-iya sa gi-scan nga barcode before
    mag-ask sa PIN; mirrors rfid_lookup() pero bukas sa bisan kinsang staff
    nga naay barcode_pin_enabled, dili lang admins."""
    data, error = get_json_or_error()
    if error:
        return error

    barcode_id = data.get("barcode_id")
    if not barcode_id:
        return jsonify({"error": "Provide barcode_id"}), 400

    conn = get_db_connection()
    try:
        staff = login_service.find_staff_for_barcode_pin_lookup(conn, barcode_id)
    finally:
        conn.close()

    if staff is None or not staff["barcode_pin_enabled"] or not staff["rfid_pin_hash"] or not staff["is_active"]:
        return jsonify({"error": "Card not recognized"}), 404

    return jsonify(
        {
            "name": full_name(staff["first_name"], staff["last_name"], staff["middle_name"], staff["suffix"]),
            "photo_path": staff["photo_path"],
        }
    )


@auth_bp.route("/login-barcode-pin", methods=["POST"])
@limiter.limit("10 per minute; 50 per hour")
def login_barcode_pin():
    """POST - Barcode + PIN login, bukas sa bisan kinsang staff nga naka-
    opt-in via Account Settings - mirrors login_rfid() pero lahi ang
    identifier (barcode_id vs rfid_id) ug ang role check
    (barcode_pin_enabled instead of admin-only)."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    barcode_id = data.get("barcode_id")
    pin = data.get("pin")

    if not barcode_id or not pin:
        return jsonify({"error": "Provide barcode_id and pin"}), 400

    # Rejects a malformed PIN before touching the DB, with the same generic error either way.
    if not pin.isdigit() or len(pin) != RFID_PIN_LENGTH:
        return jsonify({"error": "Invalid credentials"}), 401

    conn = get_db_connection()

    blocked, payload = login_service.check_existing_session_conflict(conn, request.cookies.get("session_token"))
    if blocked:
        conn.close()
        return _login_response(payload, 409)

    staff = login_service.find_staff_for_barcode_pin_login(conn, barcode_id)

    if staff is None or not staff["barcode_pin_enabled"] or not staff["rfid_pin_hash"]:
        conn.close()
        # same timing-safety trick as login() - constant-cost check either way
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
        payload, status = login_service.record_failed_attempt(conn, staff, "barcode PIN")
        conn.close()
        return _login_response(payload, status)

    login_service.reset_lockout_tracking(conn, staff["id"])
    conn.close()

    if not staff["is_active"]:
        return _login_response({"error": "This account has been deactivated"}, 403)

    assignment_error = _check_assignment_window(staff)
    if assignment_error:
        payload, status = assignment_error
        return _login_response(payload, status)

    token, csrf_token = login_service.start_session(staff["id"])

    # Distinct action name so barcode+PIN logins stay visible, not blended with password logins.
    log_action(
        staff_id=staff["id"],
        action="logged_in_barcode_pin",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Logged in via barcode + PIN: {staff['username']}",
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
    # session cookie on purpose (no max_age) - see login() above for why
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
    """POST - mag-delete sa session row sa DB ug mag-clear sa session_token
    cookie, then HX-Redirect balik sa /login."""
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
