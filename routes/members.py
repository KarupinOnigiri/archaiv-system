from flask import Blueprint, jsonify, g, request
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import parse_pagination_params, validate_lookup_id
import sqlite3
from extensions import limiter
import logging

logger = logging.getLogger(__name__)

members_bp = Blueprint("members", __name__)
VALID_MEMBER_TYPES = ("trainee", "trainer")


# # # LIST / VIEW ------------------------------------------------------------


@members_bp.route("/members")
@login_required
def view_members():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    where_clauses = []
    params = []

    q = request.args.get("q")
    if q:
        where_clauses.append(
            "(members.first_name LIKE ? OR members.last_name LIKE ? OR members.barcode_id LIKE ? OR members.member_code LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    department_id = request.args.get("department_id")
    if department_id:
        where_clauses.append("members.department_id = ?")
        params.append(department_id)

    is_restricted = request.args.get("is_restricted")
    if is_restricted is not None:
        where_clauses.append("members.is_restricted = ?")
        params.append(1 if is_restricted.lower() in ("1", "true") else 0)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM members {where_sql}", params
    ).fetchone()["cnt"]
    members = conn.execute(
        f"""
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        {where_sql}
        ORDER BY members.id LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
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
        "department_id",
        "member_type",
    ]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    conn = get_db_connection()

    error = validate_lookup_id(
        conn, "departments", data.get("department_id"), "department_id"
    )
    if error:
        conn.close()
        return error

    new_member_id = None
    generated_code = None

    try:
        cursor = conn.execute(
            """
            INSERT INTO members (first_name, last_name, middle_name, suffix, barcode_id, batch, department_id, member_type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["first_name"],
                data["last_name"],
                data.get("middle_name"),
                data.get("suffix"),
                data["barcode_id"],
                data.get("batch"),
                data["department_id"],
                data["member_type"],
            ),
        )
        new_member_id = cursor.lastrowid
        generated_code = f"MBR-{new_member_id:05d}"
        conn.execute(
            "UPDATE members SET member_code = ? WHERE id = ?",
            (generated_code, new_member_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        conn.close()
        logger.error(f"IntegrityError: {e}")
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

    return (
        jsonify(
            {
                "message": "Member added successfully",
                "id": new_member_id,
                "member_code": generated_code,
            }
        ),
        201,
    )


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

    if "department_id" in data:
        error = validate_lookup_id(
            conn, "departments", data.get("department_id"), "department_id"
        )
        if error:
            conn.close()
            return error

    old_is_restricted = member["is_restricted"]
    new_is_restricted = data.get("is_restricted", member["is_restricted"])

    if "is_restricted" in data and new_is_restricted != old_is_restricted:
        new_restriction_source = "manual" if new_is_restricted else None
    else:
        new_restriction_source = member["restriction_source"]

    if (
        "is_restricted" in data
        and old_is_restricted
        and not new_is_restricted
        and g.staff["member_id"] == id
    ):
        conn.close()
        return (
            jsonify(
                {
                    "error": "You cannot unrestrict your own account.",
                    "reason": "Ask another admin or in_charge to review it.",
                }
            ),
            403,
        )

    try:
        conn.execute(
            """
            UPDATE members
            SET first_name = ?, last_name = ?, middle_name = ?, suffix = ?, department_id = ?, batch = ?, is_restricted = ?, restriction_reason = ?, restriction_source = ?
            WHERE id = ?
            """,
            (
                data.get("first_name", member["first_name"]),
                data.get("last_name", member["last_name"]),
                data.get("middle_name", member["middle_name"]),
                data.get("suffix", member["suffix"]),
                data.get("department_id", member["department_id"]),
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
        logger.error(f"IntegrityError: {e}")
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
        logger.error(f"IntegrityError: {e}")
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

    if g.staff["member_id"] == id:
        conn.close()
        return (
            jsonify(
                {
                    "error": "You cannot unrestrict your own account.",
                    "reason": "Ask another admin or in_charge to review it.",
                }
            ),
            403,
        )

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 0, restriction_reason = NULL, restriction_source = NULL WHERE id = ?",
            (id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
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
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot deactivate member: they have an active admin account",
                    "reason": "Admin accounts cannot be deactivated this way — use /staff/<id>/request-deactivation instead, which requires peer approval.",
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
        logger.error(f"IntegrityError: {e}")
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
        logger.error(f"IntegrityError: {e}")
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


@members_bp.route("/members/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_member_by_barcode(barcode):
    conn = get_db_connection()
    member = conn.execute(
        """
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE members.barcode_id = ?
        """,
        (barcode,),
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


@members_bp.route("/members/<int:id>/restriction-history")
@login_required
def get_member_restriction_history(id):
    """Combines the audit trail of direct restrict/unrestrict actions with
    the full unrestriction_requests trail for this member — the reason
    someone was flagged, who cleared them (and how), and every request
    filed in between. Available to any logged-in staff (including
    tool_keeper) since they need this context to judge whether a member
    is ready to borrow again before filing a request."""
    conn = get_db_connection()

    member = conn.execute("SELECT id FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    restriction_events = conn.execute(
        """
        SELECT audit_log.action, audit_log.details, audit_log.created_at,
               staff.username AS performed_by
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        WHERE audit_log.target_table = 'members' AND audit_log.target_id = ?
        AND audit_log.action IN (
            'restricted_member', 'unrestricted_member',
            'auto_restricted_member', 'auto_unrestricted_member'
        )
        ORDER BY audit_log.created_at DESC
        """,
        (id,),
    ).fetchall()

    unrestriction_requests = conn.execute(
        """
        SELECT unrestriction_requests.*,
               requester.username AS requested_by_username,
               reviewer.username AS reviewed_by_username
        FROM unrestriction_requests
        JOIN staff AS requester ON unrestriction_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS reviewer ON unrestriction_requests.reviewed_by_staff_id = reviewer.id
        WHERE unrestriction_requests.member_id = ?
        ORDER BY unrestriction_requests.created_at DESC
        """,
        (id,),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "restriction_events": [dict(row) for row in restriction_events],
            "unrestriction_requests": [dict(row) for row in unrestriction_requests],
        }
    )

    # # # DEPARTMENTS (lookup table) -----------------------------------------


@members_bp.route("/departments")
@login_required
def view_departments():
    conn = get_db_connection()
    departments = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(row) for row in departments])


@members_bp.route("/departments/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_department():
    data, error = get_json_or_error()
    if error:
        return error

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing required field: name"}), 400

    conn = get_db_connection()
    try:
        cursor = conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
        conn.commit()
        new_id = cursor.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "A department with this name already exists"}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="created_department",
        target_table="departments",
        target_id=new_id,
        details=f"Created department: {name}",
    )

    return (
        jsonify(
            {"message": "Department added successfully", "id": new_id, "name": name}
        ),
        201,
    )
