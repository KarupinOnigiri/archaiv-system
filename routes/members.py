from flask import Blueprint, jsonify, g, request, render_template, make_response
import json
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

# "name" maps to two columns since there's no single stored full-name field
# -- last_name then first_name, both sorted in the same direction so the one
# header/icon in _table.html controls both.
SORTABLE_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "member_code": "members.member_code",
    "department": "departments.name",
    "member_type": "members.member_type",
    "barcode_id": "members.barcode_id",
    "batch": "members.batch",
}


def get_members_table_context(args):
    """Shared query logic for the members table -- used by both the htmx
    partial route below and the full-page /members route (routes/pages.py),
    the same way get_assets_table_context is shared on the assets side.
    Keeping this in one place means the full-page load and the htmx
    refetch can never drift out of sync on what filters/columns exist.
    Returns (context_dict, error_response)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    show_inactive = args.get("show_inactive") == "1"
    if show_inactive:
        where_clauses = ["members.is_active = 0"]
    else:
        where_clauses = ["members.is_active = 1"]
    params = []

    q = args.get("q")
    if q:
        where_clauses.append(
            "(members.first_name LIKE ? OR members.last_name LIKE ? OR members.barcode_id LIKE ? OR members.member_code LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])

    department_id = args.get("department_id")
    if department_id:
        where_clauses.append("members.department_id = ?")
        params.append(department_id)

    is_restricted = args.get("is_restricted")
    if is_restricted is not None:
        where_clauses.append("members.is_restricted = ?")
        params.append(1 if is_restricted.lower() in ("1", "true") else 0)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sort_by = args.get("sort_by", "name")
    if sort_by not in SORTABLE_COLUMNS:
        return None, (
            jsonify(
                {"error": f"sort_by must be one of: {', '.join(SORTABLE_COLUMNS)}"}
            ),
            400,
        )
    sort_dir = args.get("sort_dir", "asc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)

    # sort_column can be a single column ("members.barcode_id") or several
    # comma-separated ones (name -> last_name, first_name) -- apply the
    # chosen direction to every column in the group so one control sorts
    # all of them consistently.
    dir_sql = sort_dir.upper()
    order_by_sql = ", ".join(
        f"{col.strip()} {dir_sql}" for col in SORTABLE_COLUMNS[sort_by].split(",")
    )

    conn = get_db_connection()
    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM members
        JOIN departments ON members.department_id = departments.id
        {where_sql}
        """,
        params,
    ).fetchone()["cnt"]
    members = conn.execute(
        f"""
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        {where_sql}
        ORDER BY {order_by_sql}, members.id {dir_sql}
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return {
        "members": members,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_inactive": show_inactive,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


@members_bp.route("/members")
@login_required
def view_members():
    context, error = get_members_table_context(request.args)
    if error:
        return error

    if request.headers.get("HX-Request") == "true":
        return render_template("members/_table.html", **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["members"]],
        }
    )


@members_bp.route("/members/<int:id>/profile")
@login_required
def member_profile(id):
    """Renders the read-only profile modal -- member details plus a shell
    that the modal's own JS fills in by calling the existing
    /members/<id>/transactions and /members/<id>/restriction-history JSON
    endpoints. Those two routes return JSON (not HTML partials), matching
    every other JSON API route in this file, so the modal fetches and
    renders them client-side rather than this route trying to assemble
    everything server-side."""
    conn = get_db_connection()
    member = conn.execute(
        """
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE members.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return render_template("members/_profile_modal.html", member=member)


@members_bp.route("/members/add-form")
@roles_required("admin", "in_charge")
def add_member_form():
    conn = get_db_connection()
    departments = conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()
    conn.close()
    return render_template("members/_add_modal.html", departments=departments)


# # # CREATE ------------------------------------------------------------


@members_bp.route("/members/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_member_submit():
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    required_fields = [
        "first_name",
        "last_name",
        "barcode_id",
        "department_id",
        "member_type",
    ]
    for field in required_fields:
        if field not in data or not data[field]:
            error_msg = f"Missing required field: {field}"
            if request.is_json:
                return jsonify({"error": error_msg}), 400
            conn = get_db_connection()
            departments = conn.execute(
                "SELECT * FROM departments ORDER BY sort_order, name"
            ).fetchall()
            conn.close()
            return (
                render_template(
                    "members/_add_modal.html", departments=departments, error=error_msg
                ),
                200,
            )

    conn = get_db_connection()

    error = validate_lookup_id(
        conn, "departments", data.get("department_id"), "department_id"
    )
    if error:
        conn.close()
        return error

    if data.get("member_type") not in VALID_MEMBER_TYPES:
        conn.close()
        error_msg = f"member_type must be one of: {', '.join(VALID_MEMBER_TYPES)}"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        conn2 = get_db_connection()
        departments = conn2.execute(
            "SELECT * FROM departments ORDER BY sort_order, name"
        ).fetchall()
        conn2.close()
        return (
            render_template(
                "members/_add_modal.html", departments=departments, error=error_msg
            ),
            200,
        )

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
        error_msg = "A database constraint was violated. Check for duplicate barcode/username/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        conn2 = get_db_connection()
        departments = conn2.execute(
            "SELECT * FROM departments ORDER BY sort_order, name"
        ).fetchall()
        conn2.close()
        return (
            render_template(
                "members/_add_modal.html", departments=departments, error=error_msg
            ),
            200,
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

    if request.is_json:
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
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/members"
    return response


# # # UPDATE ------------------------------------------------------------


@members_bp.route("/members/<int:id>/edit-form")
@roles_required("admin", "in_charge")
def edit_member_form(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404
    departments = conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()
    conn.close()
    return render_template(
        "members/_edit_modal.html", member=member, departments=departments
    )


def _member_edit_error_response(payload_or_msg, status, member=None, member_id=None):
    """Returns JSON if this was a JSON request; re-renders the edit modal
    with an inline error if it was an htmx/form request. Mirrors
    _edit_error_response in routes/assets.py."""
    if request.is_json:
        return jsonify(payload_or_msg), status
    conn = get_db_connection()
    if member is None:
        member = conn.execute(
            "SELECT * FROM members WHERE id = ?", (member_id,)
        ).fetchone()
    departments = conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()
    conn.close()
    error_msg = (
        payload_or_msg.get("error")
        if isinstance(payload_or_msg, dict)
        else payload_or_msg
    )
    return (
        render_template(
            "members/_edit_modal.html",
            member=member,
            departments=departments,
            error=error_msg,
        ),
        200,
    )


@members_bp.route("/members/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_member_submit(id):
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

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
        return _member_edit_error_response(
            {
                "error": "You cannot unrestrict your own account.",
                "reason": "Ask another admin or in_charge to review it.",
            },
            403,
            member=member,
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
        return _member_edit_error_response(
            {
                "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
            },
            400,
            member=member,
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

    if request.is_json:
        return jsonify({"message": "Member updated successfully"})
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/members"
    return response


# # # RESTRICTION (manual) ------------------------------------------------


@members_bp.route("/members/<int:id>/restrict-form")
@roles_required("admin", "in_charge", "tool_keeper")
def restrict_member_form(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404
    return render_template("members/_restrict_modal.html", member=member)


@members_bp.route("/members/<int:id>/restrict", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def restrict_member(id):
    """Any front-line staff can flag a member on the spot. Requires a reason —
    unlike unrestrict, this is a low-risk/easily-reversed action, so it's
    deliberately easy to trigger, but always accountable."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()

    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    restriction_reason = data.get("restriction_reason")
    if not restriction_reason:
        conn.close()
        error_msg = "Missing required field: restriction_reason"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_restrict_modal.html", member=member, error=error_msg
            ),
            200,
        )

    if member["is_restricted"]:
        conn.close()
        error_msg = "Member is already restricted"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_restrict_modal.html", member=member, error=error_msg
            ),
            200,
        )

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'manual' WHERE id = ?",
            (restriction_reason, id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        error_msg = "A database constraint was violated. Check for duplicate barcode/username/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_restrict_modal.html", member=member, error=error_msg
            ),
            200,
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

    if request.is_json:
        return jsonify({"message": "Member restricted successfully"})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{updated_name} restricted successfully",
                "type": "success",
            },
        }
    )
    return response


@members_bp.route("/members/<int:id>/unrestrict-confirm")
@roles_required("admin", "in_charge")
def unrestrict_member_confirm(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404
    return render_template("members/_unrestrict_modal.html", member=member, error=None)


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
        error_msg = "Member is not currently restricted"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_unrestrict_modal.html", member=member, error=error_msg
            ),
            200,
        )

    if g.staff["member_id"] == id:
        conn.close()
        error_payload = {
            "error": "You cannot unrestrict your own account.",
            "reason": "Ask another admin or in_charge to review it.",
        }
        if request.is_json:
            return jsonify(error_payload), 403
        return (
            render_template(
                "members/_unrestrict_modal.html",
                member=member,
                error=error_payload["error"],
            ),
            200,
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
        error_msg = "A database constraint was violated. Check for duplicate barcode/username/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_unrestrict_modal.html", member=member, error=error_msg
            ),
            200,
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

    if request.is_json:
        return jsonify({"message": "Member unrestricted successfully"})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": "Restriction lifted",
                "type": "success",
            },
        }
    )
    return response



# # # DELETE (deactivate) -----------------------------------------------


def _deactivate_error_response(payload_or_msg, status, member):
    """Returns JSON if this was a JSON request; re-renders the deactivate
    confirm modal with an inline error if it was an htmx/form request."""
    if request.is_json:
        return jsonify(payload_or_msg), status
    error_msg = (
        payload_or_msg.get("error")
        if isinstance(payload_or_msg, dict)
        else payload_or_msg
    )
    return (
        render_template("members/_delete_modal.html", member=member, error=error_msg),
        200,
    )


@members_bp.route("/members/<int:id>/delete-confirm")
@roles_required("admin", "in_charge")
def delete_member_confirm(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404
    return render_template("members/_delete_modal.html", member=member, error=None)


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
        return _deactivate_error_response(
            {"error": "Member is already deactivated"}, 400, member
        )

    open_loan = conn.execute(
        "SELECT id FROM transactions WHERE member_id = ? AND status = 'borrowed' LIMIT 1",
        (id,),
    ).fetchone()

    if open_loan is not None:
        conn.close()
        return _deactivate_error_response(
            {
                "error": "Cannot deactivate member: they have an open borrow transaction",
                "reason": "This member currently has one or more tools checked out. They must be returned first, or contact an admin.",
            },
            409,
            member,
        )
    open_session = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (id,),
    ).fetchone()

    if open_session is not None:
        conn.close()
        return _deactivate_error_response(
            {
                "error": "Cannot deactivate member: they have an open checkout session",
                "reason": "This member has tools scanned into an unfinished checkout. Finish or cancel that session first, or contact an admin.",
            },
            409,
            member,
        )

    linked_staff = conn.execute(
        "SELECT * FROM staff WHERE member_id = ? AND is_active = 1", (id,)
    ).fetchone()

    if linked_staff is not None and g.staff["role"] != "admin":
        conn.close()
        return _deactivate_error_response(
            {
                "error": "Cannot deactivate member: they have an active staff account",
                "reason": "This member is also active staff. Only an admin can deactivate them.",
            },
            403,
            member,
        )

    if linked_staff is not None and linked_staff["role"] == "admin":
        conn.close()
        return _deactivate_error_response(
            {
                "error": "Cannot deactivate member: they have an active admin account",
                "reason": "Admin accounts cannot be deactivated this way — use /staff/<id>/request-deactivation instead, which requires peer approval.",
            },
            403,
            member,
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
        return _deactivate_error_response(
            {
                "error": "A database constraint was violated. Check for duplicate barcode/username/ID values."
            },
            400,
            member,
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

    if request.is_json:
        return jsonify({"message": "Member deactivated successfully"})
    updated_name = full_name(
        member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
    )
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{updated_name} deactivated successfully",
                "type": "success",
            },
        }
    )
    return response


@members_bp.route("/members/<int:id>/reactivate-confirm")
@roles_required("admin", "in_charge")
def reactivate_member_confirm(id):
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404
    return render_template("members/_reactivate_modal.html", member=member, error=None)


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
        error_msg = "Member is already active"
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_reactivate_modal.html", member=member, error=error_msg
            ),
            200,
        )

    try:
        conn.execute("UPDATE members SET is_active = 1 WHERE id = ?", (id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        error_msg = "A database constraint was violated. Check for duplicate barcode/username/ID values."
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "members/_reactivate_modal.html", member=member, error=error_msg
            ),
            200,
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

    if request.is_json:
        return jsonify({"message": "Member reactivated successfully"})
    updated_name = full_name(
        member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
    )
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{updated_name} reactivated successfully",
                "type": "success",
            },
        }
    )
    return response


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


@members_bp.route("/departments/manage-form")
@roles_required("admin", "in_charge")
def manage_departments_form():
    conn = get_db_connection()
    # LEFT JOIN + COUNT so departments.member_count is always a real number
    # (0 for an empty department, not just absent). The delete button in
    # _departments_modal.html reads dept.member_count to warn "N members
    # will need to be reassigned" before someone confirms a delete -- that
    # warning was previously always showing 0/nothing, since this query
    # never actually computed it.
    departments = conn.execute(
        """
        SELECT departments.*, COUNT(members.id) AS member_count
        FROM departments
        LEFT JOIN members ON members.department_id = departments.id
        GROUP BY departments.id
        ORDER BY departments.sort_order, departments.name
        """
    ).fetchall()
    conn.close()
    return render_template("members/_departments_modal.html", departments=departments)


@members_bp.route("/departments")
@login_required
def view_departments():
    conn = get_db_connection()
    departments = conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()
    conn.close()
    return jsonify([dict(row) for row in departments])


@members_bp.route("/departments/options")
@login_required
def department_options():
    """Returns just the <option> tags for the department filter dropdown on
    the Members page, in the current custom order. Fetched on demand
    whenever the Manage Departments modal fires a `departments-changed`
    event (add or reorder), so that dropdown -- which stays mounted on the
    page the whole time, unlike the add/edit member forms which re-render
    fresh from the server every time they're opened -- doesn't go stale
    without needing a full page reload."""
    conn = get_db_connection()
    departments = conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()
    conn.close()
    return render_template("members/_department_options.html", departments=departments)


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
        # New departments land at the end of the custom order by default --
        # COALESCE handles the very first department ever created, when
        # MAX(sort_order) is NULL.
        next_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM departments"
        ).fetchone()["next_order"]
        cursor = conn.execute(
            "INSERT INTO departments (name, sort_order) VALUES (?, ?)",
            (name, next_order),
        )
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


@members_bp.route("/departments/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_department(id):
    data, error = get_json_or_error()
    if error:
        return error

    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Missing required field: name"}), 400

    conn = get_db_connection()
    department = conn.execute(
        "SELECT * FROM departments WHERE id = ?", (id,)
    ).fetchone()
    if department is None:
        conn.close()
        return jsonify({"error": "Department not found"}), 404

    try:
        conn.execute("UPDATE departments SET name = ? WHERE id = ?", (name, id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "A department with this name already exists"}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="renamed_department",
        target_table="departments",
        target_id=id,
        details=f"Renamed department '{department['name']}' to '{name}'",
    )

    return jsonify({"message": "Department renamed successfully"})


@members_bp.route("/departments/reorder", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reorder_departments():
    """Persists the department order set in the Manage Departments modal.
    Expects {"order": [id, id, ...]} -- every department id, listed in the
    new top-to-bottom order. Requiring the *complete* set (rather than
    accepting a partial list) means a stale or tampered request can't
    silently orphan a department at whatever sort_order it last had."""
    data, error = get_json_or_error()
    if error:
        return error

    order = data.get("order")
    if not isinstance(order, list) or not order:
        return jsonify({"error": "order must be a non-empty list of department ids"}), 400

    try:
        ordered_ids = [int(dept_id) for dept_id in order]
    except (TypeError, ValueError):
        return jsonify({"error": "order must contain only department ids"}), 400

    if len(set(ordered_ids)) != len(ordered_ids):
        return jsonify({"error": "order contains duplicate department ids"}), 400

    conn = get_db_connection()
    existing_ids = {
        row["id"] for row in conn.execute("SELECT id FROM departments").fetchall()
    }

    if set(ordered_ids) != existing_ids:
        conn.close()
        return (
            jsonify(
                {"error": "order must include exactly the current set of departments"}
            ),
            400,
        )

    try:
        for position, dept_id in enumerate(ordered_ids):
            conn.execute(
                "UPDATE departments SET sort_order = ? WHERE id = ?",
                (position, dept_id),
            )
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        conn.close()
        logger.exception("Failed to save department order")
        return jsonify({"error": "Failed to save department order"}), 500
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="reordered_departments",
        target_table="departments",
        target_id=None,
        details=f"Reordered departments: {', '.join(str(i) for i in ordered_ids)}",
    )

    return jsonify({"message": "Department order saved successfully"})


@members_bp.route("/departments/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_department(id):
    """Deletes a department, as long as no member currently belongs to it.
    Members reference departments via a required (NOT NULL) department_id,
    so deleting out from under an assigned member isn't a safe default --
    unlike the frontend's confirm dialog (which only *warns* about affected
    members), the server refuses the delete outright and asks for members
    to be reassigned first, rather than silently orphaning or nulling out
    their department_id."""
    conn = get_db_connection()
    department = conn.execute(
        "SELECT * FROM departments WHERE id = ?", (id,)
    ).fetchone()
    if department is None:
        conn.close()
        return jsonify({"error": "Department not found"}), 404

    member_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE department_id = ?", (id,)
    ).fetchone()["cnt"]
    if member_count > 0:
        conn.close()
        noun = "member" if member_count == 1 else "members"
        return (
            jsonify(
                {
                    "error": f"Can't delete \"{department['name']}\" -- {member_count} {noun} still assigned to it. Reassign them to another department first."
                }
            ),
            400,
        )

    try:
        conn.execute("DELETE FROM departments WHERE id = ?", (id,))
        conn.commit()
    except sqlite3.IntegrityError:
        # Belt-and-suspenders: the member_count check above should already
        # prevent this, but if a foreign key constraint exists at the DB
        # level too, surface the same friendly message instead of a raw
        # 500/IntegrityError leaking out.
        conn.rollback()
        conn.close()
        return (
            jsonify({"error": "This department can't be deleted because members are still assigned to it"}),
            400,
        )
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="deleted_department",
        target_table="departments",
        target_id=id,
        details=f"Deleted department: {department['name']}",
    )

    return jsonify({"message": "Department deleted successfully"})
