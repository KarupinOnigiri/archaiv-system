import secrets
from database import get_db_connection, get_json_or_error
from werkzeug.security import check_password_hash
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, make_response, request
from utils.formatters import full_name
from utils.logger import log_action

auth_bp = Blueprint("auth", __name__)

SESSION_ABSOLUTE_HOURS = 12
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_MINUTES = 15


@auth_bp.route("/login", methods=["POST"])
def login():
    data, error = get_json_or_error()
    if error:
        return error

    username = data.get("username")
    barcode_id = data.get("barcode_id")
    password = data.get("password")

    if not password or (not username and not barcode_id):
        return jsonify({"error": "Provide username or barcode_id, plus password"}), 400

    conn = get_db_connection()

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
        return jsonify({"error": "Invalid credentials"}), 401

    # Check lockout before even touching the password
    if staff["locked_until"]:
        locked_until = datetime.strptime(staff["locked_until"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() < locked_until:
            conn.close()
            remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
            return (
                jsonify(
                    {
                        "error": f"Account locked due to too many failed attempts. Try again in {remaining} minute(s)."
                    }
                ),
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

            return (
                jsonify(
                    {
                        "error": f"Too many failed attempts. Account locked for {LOCKOUT_MINUTES} minutes."
                    }
                ),
                403,
            )

        conn.execute(
            "UPDATE staff SET failed_login_attempts = ? WHERE id = ?",
            (new_attempts, staff["id"]),
        )
        conn.commit()
        conn.close()
        return jsonify({"error": "Invalid credentials"}), 401

    # Correct password — reset lockout tracking
    conn.execute(
        "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (staff["id"],),
    )
    conn.commit()
    conn.close()

    if not staff["is_active"]:
        return jsonify({"error": "This account has been deactivated"}), 403

    if staff["role"] == "tool_keeper":
        today = date.today().isoformat()
        if staff["assigned_start"] and today < staff["assigned_start"]:
            return jsonify({"error": "Tool keeper assignment has not started yet"}), 403
        if staff["assigned_end"] and today > staff["assigned_end"]:
            return jsonify({"error": "Tool keeper assignment has expired"}), 403

    session_conn = get_db_connection()

    try:
        session_conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff["id"],))

        token = secrets.token_hex(32)
        expires_at = (
            datetime.now() + timedelta(hours=SESSION_ABSOLUTE_HOURS)
        ).strftime("%Y-%m-%d %H:%M:%S")

        session_conn.execute(
            "INSERT INTO sessions (token, staff_id, expires_at) VALUES (?, ?, ?)",
            (token, staff["id"], expires_at),
        )
        session_conn.commit()
    except Exception:
        session_conn.rollback()
        raise
    finally:
        session_conn.close()

    response = make_response(
        jsonify(
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
            }
        )
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
def logout():
    token = request.cookies.get("session_token")

    if token:
        conn = get_db_connection()
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        conn.close()

    response = make_response(jsonify({"message": "Logged out successfully"}))
    response.set_cookie(
        "session_token",
        "",
        httponly=True,
        samesite="Lax",
        max_age=0,
    )
    return response
