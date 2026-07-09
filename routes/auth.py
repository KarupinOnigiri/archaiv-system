import secrets
from database import get_db_connection, get_json_or_error
from werkzeug.security import check_password_hash
from datetime import date, datetime, timedelta
from flask import Blueprint, jsonify, make_response, request
from utils.formatters import full_name

auth_bp = Blueprint("auth", __name__)


SESSION_ABSOLUTE_HOURS = 12


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

    conn.close()

    if staff is None:
        return jsonify({"error": "Invalid credentials"}), 401

    if not check_password_hash(staff["password_hash"], password):
        return jsonify({"error": "Invalid credentials"}), 401

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
        # Enforce single active session per staff — wipe any existing session first
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
