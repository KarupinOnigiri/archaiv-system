from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.validators import validate_date_string
from utils.formatters import full_name
from werkzeug.security import generate_password_hash
import sqlite3

staff_bp = Blueprint("staff", __name__)


# # # LIST / VIEW ------------------------------------------------------------


@staff_bp.route("/staff")
@login_required
def view_staff():
    conn = get_db_connection()
    staff = conn.execute("""
        SELECT staff.id, staff.member_id, staff.username, staff.role, staff.assigned_start,
               staff.assigned_end, staff.is_active, staff.created_by_staff_id, staff.created_at,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, members.department
        FROM staff
        JOIN members ON staff.member_id = members.id
        """).fetchall()
    conn.close()
    return jsonify([dict(row) for row in staff])


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

    for field in ("assigned_start", "assigned_end"):
        error = validate_date_string(data.get(field), field)
        if error:
            return error

    conn = get_db_connection()
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
        return jsonify({"error": f"Database error: {str(e)}"}), 400

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

    for field in ("assigned_start", "assigned_end"):
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error

    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    try:
        conn.execute(
            """
            UPDATE staff
            SET role = ?, assigned_start = ?, assigned_end = ?
            WHERE id = ?
            """,
            (
                data.get("role", staff["role"]),
                data.get("assigned_start", staff["assigned_start"]),
                data.get("assigned_end", staff["assigned_end"]),
                staff["id"],
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 400
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


# # # DELETE (deactivate, admin only) ------------------------------------


@staff_bp.route("/staff/<int:id>/delete", methods=["POST"])
@roles_required("admin")
def delete_staff(id):
    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    try:
        conn.execute("UPDATE staff SET is_active = 0 WHERE id = ?", (id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        return jsonify({"error": f"Database error: {str(e)}"}), 400
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


# # # API ROUTES (barcode-based staff lookup for login) -----------------


@staff_bp.route("/api/v1/staff/barcode/<string:barcode>")
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
