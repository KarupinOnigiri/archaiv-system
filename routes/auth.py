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
from services.auth_service import login_required, SESSION_IDLE_MINUTES
from extensions import limiter

auth_bp = Blueprint("auth", __name__)

SESSION_ABSOLUTE_HOURS = 12
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15

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
                minutes=SESSION_IDLE_MINUTES
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
    else:
        staff = conn.execute(
            """
            SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
            FROM staff
            JOIN members ON staff.member_id = members.id
            WHERE members.barcode_id = ?
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

    # Check lockout before even touching the password
    if staff["locked_until"]:
        locked_until = datetime.strptime(staff["locked_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < locked_until:
            conn.close()
            remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
            # return (
            #     jsonify(
            #         {
            #             "error": f"Account locked due to too many failed attempts. Try again in {remaining} minute(s)."
            #         }
            #     ),
            #     403,
            # )
            return _login_response(
                {
                    "error": f"Account locked due to too many failed attempts. Try again in {remaining} minute(s)."
                },
                403,
            )

    if not check_password_hash(staff["password_hash"], password):
        new_attempts = staff["failed_login_attempts"] + 1

        if new_attempts >= MAX_FAILED_ATTEMPTS:
            lockout_until = (
                datetime.now() + timedelta(minutes=LOCKOUT_MINUTES)
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
                details=f"Account locked after {new_attempts} failed login attempts",
            )

            # return (
            #     jsonify(
            #         {
            #             "error": f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
            #         }
            #     ),
            #     403,
            # )
            return _login_response(
                {
                    "error": f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
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
        expires_at = (
            datetime.now() + timedelta(hours=SESSION_ABSOLUTE_HOURS)
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
        max_age=SESSION_ABSOLUTE_HOURS * 3600,
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
        max_age=0,
    )
    return response
