from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import parse_pagination_params
import sqlite3
from extensions import limiter

members_bp = Blueprint("members", __name__)


# # # LIST / VIEW ------------------------------------------------------------


@members_bp.route("/members")
@login_required
def view_members():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    total_count = conn.execute("SELECT COUNT(*) AS cnt FROM members").fetchone()["cnt"]
    members = conn.execute(
        "SELECT * FROM members ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in members],
        }
    )


# # # CREATE ------------------------------------------------------------


@members_bp.route("/members/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
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
        action="created_member",
        target_table="members",
        target_id=new_member_id,
        details=f"Created member {full_name(data['first_name'], data['last_name'], data.get('middle_name'), data.get('suffix'))}",
    )

    return jsonify({"message": "Member added successfully", "id": new_member_id}), 201


# # # UPDATE ------------------------------------------------------------


@members_bp.route("/members/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
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

    if "is_restricted" in data and new_is_restricted != old_is_restricted:
        new_restriction_source = "manual" if new_is_restricted else None
    else:
        new_restriction_source = member["restriction_source"]

    try:

        conn.execute(
            """
            UPDATE members
            SET first_name = ?, last_name = ?, middle_name = ?, suffix = ?, department = ?, batch = ?, is_restricted = ?, restriction_reason = ?, restriction_source = ?
            WHERE id = ?
            """,
            (
                data.get("first_name", member["first_name"]),
                data.get("last_name", member["last_name"]),
                data.get("middle_name", member["middle_name"]),
                data.get("suffix", member["suffix"]),
                data.get("department", member["department"]),
                data.get("batch", member["batch"]),
                new_is_restricted,
                data.get("restriction_reason", member["restriction_reason"]),
                new_restriction_source,
                member["id"],
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

    return jsonify({"message": "Member updated successfully"})


# # # RESTRICTION (manual) ------------------------------------------------


@members_bp.route("/members/<int:id>/restrict", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def restrict_member(id):
    """Any front-line staff can flag a member on the spot. Requires a reason —
    unlike unrestrict, this is a low-risk/easily-reversed action, so it's
    deliberately easy to trigger, but always accountable."""
    data, error = get_json_or_error()
    if error:
        return error

    restriction_reason = data.get("restriction_reason")
    if not restriction_reason:
        return jsonify({"error": "Missing required field: restriction_reason"}), 400

    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if member["is_restricted"]:
        conn.close()
        return jsonify({"error": "Member is already restricted"}), 400

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'manual' WHERE id = ?",
            (restriction_reason, id),
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

    updated_name = full_name(
        member["first_name"],
        member["last_name"],
        member["middle_name"],
        member["suffix"],
    )
    log_action(
        staff_id=g.staff["id"],
        action="restricted_member",
        target_table="members",
        target_id=id,
        details=f"Restricted member {updated_name}: {restriction_reason}",
    )

    return jsonify({"message": "Member restricted successfully"})


@members_bp.route("/members/<int:id>/unrestrict", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def unrestrict_member(id):
    """Lifting a restriction re-grants borrowing access, so it deliberately
    requires more authority than restricting — tool_keeper cannot call this."""
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_restricted"]:
        conn.close()
        return jsonify({"error": "Member is not currently restricted"}), 400

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 0, restriction_reason = NULL, restriction_source = NULL WHERE id = ?",
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

    updated_name = full_name(
        member["first_name"],
        member["last_name"],
        member["middle_name"],
        member["suffix"],
    )
    log_action(
        staff_id=g.staff["id"],
        action="unrestricted_member",
        target_table="members",
        target_id=id,
        details=f"Lifted restriction on member {updated_name}",
    )

    return jsonify({"message": "Member unrestricted successfully"})


# # # DELETE (deactivate) -----------------------------------------------


@members_bp.route("/members/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_member(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if not member["is_active"]:
        conn.close()
        return jsonify({"error": "Member is already deactivated"}), 400

    open_loan = conn.execute(
        "SELECT id FROM transactions WHERE member_id = ? AND status = 'borrowed' LIMIT 1",
        (id,),
    ).fetchone()

    if open_loan is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot deactivate member: they have an open borrow transaction",
                    "reason": "This member currently has one or more tools checked out. They must be returned first, or contact an admin.",
                }
            ),
            409,
        )
      open_session = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (id,),
    ).fetchone()

    if open_session is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot deactivate member: they have an open checkout session",
                    "reason": "This member has tools scanned into an unfinished checkout. Finish or cancel that session first, or contact an admin.",
                }
            ),
            409,
        )  

    linked_staff = conn.execute(
        "SELECT * FROM staff WHERE member_id = ? AND is_active = 1", (id,)
    ).fetchone()

    if linked_staff is not None and g.staff["role"] != "admin":
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot deactivate member: they have an active staff account",
                    "reason": "This member is also active staff. Only an admin can deactivate them.",
                }
            ),
            403,
        )

    if linked_staff is not None and linked_staff["role"] == "admin":
        remaining_admins = conn.execute(
            "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'admin' AND is_active = 1 AND id != ?",
            (linked_staff["id"],),
        ).fetchone()["cnt"]
        if remaining_admins == 0:
            conn.close()
            return (
                jsonify(
                    {
                        "error": "Cannot deactivate member: this is the last active admin account"
                    }
                ),
                403,
            )

    try:
        conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (id,))
        if linked_staff is not None:
            conn.execute(
                "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL WHERE id = ?",
                (linked_staff["id"],),
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
        action="deactivated_member",
        target_table="members",
        target_id=id,
        details=f"Deactivated member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
    )

    if linked_staff is not None:
        log_action(
            staff_id=g.staff["id"],
            action="deactivated_staff_cascade",
            target_table="staff",
            target_id=linked_staff["id"],
            details=f"Staff account {linked_staff['username']} auto-deactivated because linked member was deactivated",
        )

    return jsonify({"message": "Member deactivated successfully"})


@members_bp.route("/members/<int:id>/reactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reactivate_member(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    if member["is_active"]:
        conn.close()
        return jsonify({"error": "Member is already active"}), 400

    try:
        conn.execute("UPDATE members SET is_active = 1 WHERE id = ?", (id,))
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
        action="reactivated_member",
        target_table="members",
        target_id=id,
        details=f"Reactivated member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
    )

    return jsonify({"message": "Member reactivated successfully"})


# # # API ROUTES (scanning lookups) ------------------------------------------


@members_bp.route("/api/v1/members/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_member_by_barcode(barcode):
    conn = get_db_connection()
    member = conn.execute(
        "SELECT * FROM members WHERE barcode_id = ?", (barcode,)
    ).fetchone()
    conn.close()

    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return jsonify(dict(member))


@members_bp.route("/members/<int:id>/transactions")
@login_required
def get_member_transactions(id):
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()

    member = conn.execute("SELECT id FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    status_filter = request.args.get("status")

    where_clause = "WHERE transactions.member_id = ?"
    params = [id]

    if status_filter == "current":
        where_clause += " AND transactions.status = 'borrowed'"
    elif status_filter == "returned":
        where_clause += " AND transactions.status = 'returned'"

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM transactions {where_clause}", params
    ).fetchone()["cnt"]

    transactions = conn.execute(
        f"""
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        {where_clause}
        ORDER BY transactions.time_out DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in transactions],
        }
    )
