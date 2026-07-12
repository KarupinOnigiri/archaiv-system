from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.validators import (
    validate_date_string,
    validate_date_fields,
    validate_password_strength,
    parse_pagination_params,
)
from utils.formatters import full_name
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3

staff_bp = Blueprint("staff", __name__)


# # # LIST / VIEW ------------------------------------------------------------


@staff_bp.route("/staff")
@login_required
def view_staff():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    total_count = conn.execute("SELECT COUNT(*) AS cnt FROM staff").fetchone()["cnt"]
    staff = conn.execute(
        """
        SELECT staff.id, staff.member_id, staff.username, staff.role, staff.assigned_start,
               staff.assigned_end, staff.is_active, staff.created_by_staff_id, staff.created_at,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, members.department
        FROM staff
        JOIN members ON staff.member_id = members.id
        ORDER BY staff.id
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()
    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in staff],
        }
    )


@staff_bp.route("/staff/<int:id>")
@login_required
def get_staff_by_id(id):
    conn = get_db_connection()
    staff = conn.execute(
        """
        SELECT staff.id, staff.member_id, staff.username, staff.role, staff.assigned_start,
               staff.assigned_end, staff.is_active, staff.created_by_staff_id, staff.created_at,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, members.department
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if staff is None:
        return jsonify({"error": "Staff not found"}), 404

    return jsonify(dict(staff))


@staff_bp.route("/audit-log")
@login_required
def view_audit_log():
    """Read-only audit trail. admin can view everyone's entries and filter
    by any staff_id. in_charge and tool_keeper can only view their own —
    any staff_id they pass in is ignored and forced to their own id.
    Other filters are all optional and combine with AND.
    limit defaults to 200 and is capped at 1000 to avoid dumping the whole
    table on a growing dataset."""
    filters = []
    params = []

    if g.staff["role"] == "admin":
        staff_id = request.args.get("staff_id")
        if staff_id:
            filters.append("audit_log.staff_id = ?")
            params.append(staff_id)
    else:
        filters.append("audit_log.staff_id = ?")
        params.append(g.staff["id"])

    action = request.args.get("action")
    if action:
        filters.append("audit_log.action = ?")
        params.append(action)

    target_table = request.args.get("target_table")
    if target_table:
        filters.append("audit_log.target_table = ?")
        params.append(target_table)

    since = request.args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return error
        filters.append("audit_log.created_at >= ?")
        params.append(since)

    until = request.args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return error
        filters.append("audit_log.created_at <= ?")
        params.append(until)

    try:
        limit = int(request.args.get("limit", 200))
    except ValueError:
        return jsonify({"error": "limit must be an integer"}), 400
    limit = max(1, min(limit, 1000))

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    conn = get_db_connection()
    logs = conn.execute(
        f"""
        SELECT audit_log.*, staff.username AS staff_username
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        {where_clause}
        ORDER BY audit_log.created_at DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    conn.close()

    return jsonify([dict(row) for row in logs])


# # # CREATE (admin only) ------------------------------------------------


@staff_bp.route("/staff/add", methods=["POST"])
@roles_required("admin")
def add_staff_submit():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["username", "password", "role", "member_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    error = validate_date_fields(data, ("assigned_start", "assigned_end"))
    if error:
        return error

    error = validate_password_strength(data["password"])
    if error:
        return error

    conn = get_db_connection()

    member = conn.execute(
        "SELECT id FROM members WHERE id = ?", (data["member_id"],)
    ).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "member_id does not match an existing member"}), 404

    hashed_password = generate_password_hash(data["password"])
    new_staff_id = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO staff (member_id, username, password_hash, role, assigned_start, assigned_end, is_active, created_by_staff_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data.get("member_id"),
                data["username"],
                hashed_password,
                data["role"],
                data.get("assigned_start"),
                data.get("assigned_end"),
                data.get("is_active", 1),
                g.staff["id"],
            ),
        )
        conn.commit()
        new_staff_id = cursor.lastrowid
    except sqlite3.IntegrityError as e:
        conn.close()
        print(f"IntegrityError: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )

    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_staff",
        target_table="staff",
        target_id=new_staff_id,
        details=f"Created staff account: {data['username']}",
    )

    return jsonify({"message": "Staff added successfully", "id": new_staff_id}), 201


# # # UPDATE ------------------------------------------------------------


@staff_bp.route("/staff/<int:id>/edit", methods=["POST"])
@roles_required("admin")
def edit_staff_submit(id):
    data, error = get_json_or_error()
    if error:
        return error

    error = validate_date_fields(data, ("assigned_start", "assigned_end"))
    if error:
        return error

    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    new_role = data.get("role", staff["role"])
    if staff["role"] == "admin" and new_role != "admin":
        remaining_admins = conn.execute(
            "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'admin' AND is_active = 1 AND id != ?",
            (id,),
        ).fetchone()["cnt"]
        if remaining_admins == 0:
            conn.close()
            return (
                jsonify(
                    {
                        "error": "Cannot change role: this is the last active admin account"
                    }
                ),
                403,
            )
    try:
        conn.execute(
            """
            UPDATE staff
            SET role = ?, assigned_start = ?, assigned_end = ?
            WHERE id = ?
            """,
            (
                # data.get("role", staff["role"]),
                new_role,
                data.get("assigned_start", staff["assigned_start"]),
                data.get("assigned_end", staff["assigned_end"]),
                staff["id"],
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"IntegrityError: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_staff",
        target_table="staff",
        target_id=staff["id"],
        details=f"Updated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff updated successfully"})


# # # PASSWORD MANAGEMENT ------------------------------------------------


@staff_bp.route("/staff/change-password", methods=["POST"])
@login_required
def change_password_submit():
    """Self-service password change. Requires the current password to
    prove the request comes from someone who already has legitimate
    access, not just an active hijacked session."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    new_password = data.get("new_password")

    if not current_password or not new_password:
        return (
            jsonify({"error": "current_password and new_password are required"}),
            400,
        )

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    error = validate_password_strength(new_password, "new_password")
    if error:
        return error

    new_hash = generate_password_hash(new_password)
    current_token = request.cookies.get("session_token")

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE staff SET password_hash = ? WHERE id = ?",
            (new_hash, g.staff["id"]),
        )
        # kill every other active session for this account, but keep the
        # one making this request so the staff member isn't logged out
        conn.execute(
            "DELETE FROM sessions WHERE staff_id = ? AND token != ?",
            (g.staff["id"], current_token),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="changed_own_password",
        target_table="staff",
        target_id=g.staff["id"],
        details=f"Staff {g.staff['username']} changed their own password",
    )

    return jsonify({"message": "Password changed successfully"})


@staff_bp.route("/staff/<int:id>/reset-password", methods=["POST"])
@roles_required("admin")
def reset_password_submit(id):
    """Admin-initiated reset for forgotten passwords. No current password
    needed since this is the recovery path — but every existing session
    for the account is killed, since a reset usually means the account
    may have been locked out or compromised."""
    data, error = get_json_or_error()
    if error:
        return error

    new_password = data.get("new_password")
    if not new_password:
        return jsonify({"error": "Missing required field: new_password"}), 400

    error = validate_password_strength(new_password, "new_password")
    if error:
        return error

    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    new_hash = generate_password_hash(new_password)

    try:
        conn.execute("UPDATE staff SET password_hash = ? WHERE id = ?", (new_hash, id))
        conn.execute("DELETE FROM sessions WHERE staff_id = ?", (id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="reset_staff_password",
        target_table="staff",
        target_id=id,
        details=f"Admin reset password for staff account: {staff['username']}",
    )

    return jsonify({"message": "Password reset successfully"})


# # # DELETE (deactivate, admin only) ------------------------------------


@staff_bp.route("/staff/<int:id>/delete", methods=["POST"])
@roles_required("admin")
def delete_staff(id):
    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if staff["role"] == "admin":
        conn.close()
        return jsonify({"error": "Admin account/s cannot be deactivated"}), 403

    if not staff["is_active"]:
        conn.close()
        return jsonify({"error": "Staff account is already deactivated"}), 400

    try:
        conn.execute(
            "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL WHERE id = ?",
            (id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        print(f"IntegrityError: {e}")
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deactivated_staff",
        target_table="staff",
        target_id=id,
        details=f"Deactivated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff deactivated successfully"})


@staff_bp.route("/staff/<int:id>/reactivate", methods=["POST"])
@roles_required("admin")
def reactivate_staff(id):
    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if staff["is_active"]:
        conn.close()
        return jsonify({"error": "Staff account is already active"}), 400

    try:
        conn.execute("UPDATE staff SET is_active = 1 WHERE id = ?", (id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return (
            jsonify(
                {
                    "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
                }
            ),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="reactivated_staff",
        target_table="staff",
        target_id=id,
        details=f"Reactivated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff reactivated successfully"})


# # # API ROUTES (barcode-based staff lookup for login) -----------------


@staff_bp.route("/api/v1/staff/barcode/<string:barcode>")
@login_required
def get_staff_by_barcode(barcode):
    conn = get_db_connection()
    result = conn.execute(
        """
        SELECT staff.id, staff.role, staff.is_active, staff.assigned_start, staff.assigned_end,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, members.department
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE members.barcode_id = ?
        """,
        (barcode,),
    ).fetchone()
    conn.close()

    if result is None:
        return jsonify({"error": "Staff not found"}), 404

    return jsonify(dict(result))
