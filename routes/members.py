from flask import Blueprint, jsonify, g
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.formatters import full_name
import sqlite3

members_bp = Blueprint("members", __name__)


# # # LIST / VIEW ------------------------------------------------------------


@members_bp.route("/members")
@login_required
def view_members():
    conn = get_db_connection()

    members = conn.execute("SELECT * FROM members").fetchall()
    conn.close()
    return jsonify([dict(row) for row in members])


# # # CREATE ------------------------------------------------------------


@members_bp.route("/members/add", methods=["POST"])
@roles_required("admin", "in_charge")
def add_member_submit():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = [
        "first_name",
        "last_name",
        "barcode_id",
        "department",
        "member_type",
    ]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    conn = get_db_connection()
    new_member_id = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO members (first_name, last_name, middle_name, suffix, barcode_id, batch, department, member_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["first_name"],
                data["last_name"],
                data.get("middle_name"),
                data.get("suffix"),
                data["barcode_id"],
                data.get("batch"),
                data["department"],
                data["member_type"],
            ),
        )
        conn.commit()
        new_member_id = cursor.lastrowid
    except sqlite3.IntegrityError as e:
        conn.close()
        return jsonify({"error": f"Database error: {str(e)}"}), 400

    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_member",
        target_table="members",
        target_id=new_member_id,
        details=f"Created member {full_name(data['first_name'], data['last_name'], data.get('middle_name'), data.get('suffix'))}",
    )

    return jsonify({"message": "Member added successfully", "id": new_member_id}), 201


# # # UPDATE ------------------------------------------------------------


@members_bp.route("/members/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
def edit_member_submit(id):
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    old_is_restricted = member["is_restricted"]
    new_is_restricted = data.get("is_restricted", member["is_restricted"])

    conn.execute(
        """
        UPDATE members
        SET first_name = ?, last_name = ?, middle_name = ?, suffix = ?, department = ?, batch = ?, is_active = ?, is_restricted = ?, restriction_reason = ?
        WHERE id = ?
        """,
        (
            data.get("first_name", member["first_name"]),
            data.get("last_name", member["last_name"]),
            data.get("middle_name", member["middle_name"]),
            data.get("suffix", member["suffix"]),
            data.get("department", member["department"]),
            data.get("batch", member["batch"]),
            data.get("is_active", member["is_active"]),
            new_is_restricted,
            data.get("restriction_reason", member["restriction_reason"]),
            member["id"],
        ),
    )
    conn.commit()

    updated_name = full_name(
        data.get("first_name", member["first_name"]),
        data.get("last_name", member["last_name"]),
        data.get("middle_name", member["middle_name"]),
        data.get("suffix", member["suffix"]),
    )
    log_action(
        staff_id=g.staff["id"],
        action="updated_member",
        target_table="members",
        target_id=member["id"],
        details=f"Updated member {updated_name}",
    )

    if old_is_restricted != new_is_restricted:
        if new_is_restricted:
            log_action(
                staff_id=g.staff["id"],
                action="restricted_member",
                target_table="members",
                target_id=member["id"],
                details=f"Restricted member {updated_name}: {data.get('restriction_reason', 'No reason given')}",
            )
        else:
            log_action(
                staff_id=g.staff["id"],
                action="unrestricted_member",
                target_table="members",
                target_id=member["id"],
                details=f"Lifted restriction on member {updated_name}",
            )

    conn.close()
    return jsonify({"message": "Member updated successfully"})


# # # DELETE (deactivate) -----------------------------------------------


@members_bp.route("/members/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
def delete_member(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deactivated_member",
        target_table="members",
        target_id=id,
        details=f"Deactivated member {member['name']}",
    )

    return jsonify({"message": "Member deactivated successfully"})


# # # API ROUTES (scanning lookups) ------------------------------------------


@members_bp.route("/api/v1/members/barcode/<string:barcode>")
def get_member_by_barcode(barcode):
    conn = get_db_connection()
    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (barcode,)
    ).fetchone()
    conn.close()

    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return jsonify(dict(member))
