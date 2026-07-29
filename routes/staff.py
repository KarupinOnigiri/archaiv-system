from flask import Blueprint, jsonify, g, request, render_template
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.admin_deactivation_service import (
    determine_approval_tier,
    finalize_if_ready,
)
from utils.logger import log_action
from utils.validators import (
    validate_date_string,
    validate_date_fields,
    validate_password_strength,
    parse_pagination_params,
)
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3
from extensions import limiter
import logging

logger = logging.getLogger(__name__)

staff_bp = Blueprint("staff", __name__)
VALID_ROLES = ("admin", "in_charge", "tool_keeper")


# # # LIST / VIEW ------------------------------------------------------------


def get_staff_table_context(args):
    """Shared query logic for the staff table -- used by both the htmx
    partial route below and the full-page /staff route (routes/pages.py),
    the same way get_members_table_context is shared on the members side.
    Keeping this in one place means the full-page load and the htmx
    refetch can never drift out of sync on what filters exist.
    Returns (context_dict, error_response)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    q = args.get("q", "").strip()
    role = args.get("role")
    department_id = args.get("department_id")
    show_inactive = args.get("show_inactive") == "1"
    # `status` is the fine-grained filter the stat cards drive (active vs.
    # locked vs. inactive) -- distinct from `show_inactive`, which only
    # toggles whether inactive accounts are included alongside everyone
    # else. Without this, "Active" and "Locked" had no way to isolate
    # themselves from each other (both are is_active=1 rows) or from the
    # unfiltered list, so clicking those cards sent a `status` param the
    # backend silently ignored and the table never visibly changed.
    status = args.get("status")
    valid_statuses = ("active", "locked", "inactive")

    if role and role not in VALID_ROLES:
        return None, (
            jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}),
            400,
        )

    if status and status not in valid_statuses:
        return None, (
            jsonify({"error": f"status must be one of: {', '.join(valid_statuses)}"}),
            400,
        )

    # `status`, when given, fully determines the active/locked/inactive
    # slice and takes precedence over the plainer show_inactive-only check
    # below -- e.g. status=locked should still isolate locked accounts even
    # if show_inactive happens to be left off in the request.
    if status == "inactive":
        where = ["staff.is_active = 0"]
    elif status == "active":
        where = [
            "staff.is_active = 1",
            "(staff.locked_until IS NULL OR staff.locked_until <= datetime('now', 'localtime'))",
        ]
    elif status == "locked":
        where = [
            "staff.is_active = 1",
            "staff.locked_until IS NOT NULL",
            "staff.locked_until > datetime('now', 'localtime')",
        ]
    else:
        where = [] if show_inactive else ["staff.is_active = 1"]
    params = []
    if q:
        where.append(
            "(staff.username LIKE ? OR staff.staff_code LIKE ? OR members.first_name LIKE ? OR members.last_name LIKE ?)"
        )
        like = f"%{q}%"
        params.extend([like, like, like, like])
    if role:
        where.append("staff.role = ?")
        params.append(role)
    if department_id:
        where.append("members.department_id = ?")
        params.append(department_id)
    where_clause = f"WHERE {' AND '.join(where)}" if where else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt
        FROM staff
        JOIN members ON staff.member_id = members.id
        {where_clause}
        """,
        params,
    ).fetchone()["cnt"]
    # Named staff_list (not "staff") so it doesn't collide with the
    # logged-in-user variable that staff.html and staff/_table.html both
    # refer to as `staff` (e.g. `{% if staff.role == "admin" %}`).
    staff_list = conn.execute(
        f"""
        SELECT staff.id, staff.member_id, staff.username, staff.staff_code, staff.role,
               staff.assigned_start, staff.assigned_end, staff.is_active, staff.locked_until,
               staff.failed_login_attempts, staff.created_by_staff_id, staff.created_at,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        {where_clause}
        ORDER BY staff.id
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return {
        "staff_list": staff_list,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_inactive": show_inactive,
        "status": status or "",
    }, None


@staff_bp.route("/staff")
@login_required
def view_staff():
    context, error = get_staff_table_context(request.args)
    if error:
        return error

    if request.headers.get("HX-Request") == "true":
        # staff=g.staff is required here (not just for the full-page load)
        # -- staff/_table.html's `can_manage` check and its per-row Actions
        # dropdown both read the bare `staff` variable, which would
        # otherwise be undefined on every htmx filter/search/pagination
        # swap and silently hide admin controls after the first load.
        return render_template("staff/_table.html", staff=g.staff, **context)

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["staff_list"]],
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
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
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
    Other filters are all optional and combine with AND. Pagination uses
    the shared limit/offset helper (default 25, capped at 100 — same as
    every other paginated route), not a route-specific limit."""
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

    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    conn = get_db_connection()

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM audit_log {where_clause}", params
    ).fetchone()["cnt"]

    logs = conn.execute(
        f"""
        SELECT audit_log.*, staff.username AS staff_username
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        {where_clause}
        ORDER BY audit_log.created_at DESC
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
            "results": [dict(row) for row in logs],
        }
    )


# # # CREATE (admin only) ------------------------------------------------


@staff_bp.route("/staff/add-form")
@roles_required("admin")
def add_staff_form():
    """Renders the "Add New Staff" modal. A staff account is access
    granted to an existing member record (staff.member_id is UNIQUE NOT
    NULL) -- there's no separate name/department to collect here, since
    those already live on `members`.

    The member picker is a type-ahead search (see
    eligible_members_search below) rather than a preloaded <select> --
    with enough members, dumping every eligible one into a dropdown on
    every modal open doesn't scale and makes picking the right one
    tedious. This route no longer needs to query members at all."""
    return render_template("staff/_add_modal.html")


@staff_bp.route("/staff/eligible-members-search")
@roles_required("admin")
def eligible_members_search():
    """Backs the Add Staff modal's member picker. Matches name, member
    code, or barcode ID (so scanning a barcode into the same box works
    just as well as typing a name), restricted to the same
    active-and-not-already-staff set add_staff_form's eligibility rule
    requires. Capped at a handful of results since this is a live-as-
    you-type search, not a full listing -- an empty query intentionally
    returns nothing rather than the whole table."""
    q = request.args.get("q", "").strip()
    if not q:
        return render_template("staff/_eligible_member_results.html", results=[], q=q)

    conn = get_db_connection()
    like = f"%{q}%"
    results = conn.execute(
        """
        SELECT members.id, members.first_name, members.last_name, members.middle_name,
               members.suffix, members.member_code, members.barcode_id,
               departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE members.is_active = 1
          AND members.id NOT IN (SELECT member_id FROM staff)
          AND (
            members.first_name LIKE ? OR members.last_name LIKE ?
            OR members.member_code LIKE ? OR members.barcode_id LIKE ?
          )
        ORDER BY members.last_name, members.first_name
        LIMIT 8
        """,
        (like, like, like, like),
    ).fetchall()
    conn.close()

    return render_template("staff/_eligible_member_results.html", results=results, q=q)



@staff_bp.route("/staff/add", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def add_staff_submit():
    data, error = get_json_or_error()
    if error:
        return error

    required_fields = ["username", "password", "role", "member_id"]
    for field in required_fields:
        if field not in data or not data[field]:
            return jsonify({"error": f"Missing required field: {field}"}), 400

    if data["role"] not in VALID_ROLES:
        return (
            jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}),
            400,
        )

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
    generated_code = None

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
        new_staff_id = cursor.lastrowid
        generated_code = f"STF-{new_staff_id:05d}"
        conn.execute(
            "UPDATE staff SET staff_code = ? WHERE id = ?",
            (generated_code, new_staff_id),
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
        action="created_staff",
        target_table="staff",
        target_id=new_staff_id,
        details=f"Created staff account: {data['username']}",
    )

    return (
        jsonify(
            {
                "message": "Staff added successfully",
                "id": new_staff_id,
                "staff_code": generated_code,
            }
        ),
        201,
    )


# # # UPDATE ------------------------------------------------------------


@staff_bp.route("/staff/<int:id>/edit-form")
@roles_required("admin")
def edit_staff_form(id):
    """Renders the "Edit Staff" modal. Only role and assigned_start/end are
    actually editable here -- matches edit_staff_submit below, which never
    touches username or anything on the linked member record. Name/
    department/staff_code are shown read-only for context."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.username, staff.role, staff.assigned_start, staff.assigned_end,
               staff.staff_code, staff.is_active, staff.locked_until,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.member_code, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if staff_member is None:
        return jsonify({"error": "Staff not found"}), 404

    return render_template("staff/_edit_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/edit", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def edit_staff_submit(id):
    data, error = get_json_or_error()
    if error:
        return error

    if "role" in data and data["role"] not in VALID_ROLES:
        return (
            jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}),
            400,
        )

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
        conn.close()
        return (
            jsonify(
                {
                    "error": "Cannot change an admin's role directly",
                    "reason": "Demoting or deactivating an admin requires peer approval — use /staff/<id>/request-deactivation instead.",
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
        action="updated_staff",
        target_table="staff",
        target_id=staff["id"],
        details=f"Updated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff updated successfully"})


# # # PASSWORD MANAGEMENT ------------------------------------------------


@staff_bp.route("/staff/change-password", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
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
@limiter.limit("10 per minute; 30 per hour")
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


@staff_bp.route("/staff/<int:id>/unlock", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def unlock_staff(id):
    """Manually clears a lockout before it would naturally expire. There's
    no self-service/email recovery path in this offline deployment, so if
    the one admin on shift gets locked out, someone with another admin
    account needs a way to clear it immediately rather than waiting out
    LOCKOUT_MINUTES (see auth.py)."""
    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if not staff["locked_until"] and staff["failed_login_attempts"] == 0:
        conn.close()
        return jsonify({"error": "This account is not currently locked"}), 400

    conn.execute(
        "UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?",
        (id,),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="unlocked_staff",
        target_table="staff",
        target_id=id,
        details=f"Manually cleared lockout for staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff account unlocked successfully"})


# # # DELETE (deactivate, admin only) ------------------------------------


@staff_bp.route("/staff/<int:id>/delete-confirm")
@roles_required("admin")
def delete_staff_form(id):
    """Renders the deactivate-confirmation modal. Admin rows never reach
    this in practice -- staff/_table.html only wires the "Deactivate" link
    up for non-admin rows, showing "Request Deactivation" instead for
    admins -- but the same guard delete_staff() enforces is repeated here
    so a direct hit to this URL can't be used to work around that."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()

    if staff_member is None:
        return jsonify({"error": "Staff not found"}), 404

    if staff_member["role"] == "admin":
        return jsonify({"error": "Admin account/s cannot be deactivated"}), 403

    return render_template("staff/_delete_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/delete", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
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
        action="deactivated_staff",
        target_table="staff",
        target_id=id,
        details=f"Deactivated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff deactivated successfully"})


@staff_bp.route("/staff/<int:id>/reactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
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
        action="reactivated_staff",
        target_table="staff",
        target_id=id,
        details=f"Reactivated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff reactivated successfully"})


# # # API ROUTES (barcode-based staff lookup for login) -----------------


@staff_bp.route("/staff/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_staff_by_barcode(barcode):
    conn = get_db_connection()
    result = conn.execute(
        """
        SELECT staff.id, staff.role, staff.is_active, staff.assigned_start, staff.assigned_end,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE members.barcode_id = ?
        """,
        (barcode,),
    ).fetchone()
    conn.close()

    if result is None:
        return jsonify({"error": "Staff not found"}), 404

    return jsonify(dict(result))


# # # ADMIN DEACTIVATION (approval-gated) ----------------------------------


@staff_bp.route("/staff/<int:id>/request-deactivation", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def request_admin_deactivation(id):
    """Requests deactivation of a fellow admin account instead of doing it
    unilaterally — /staff/<id>/delete refuses this for any admin. Takes
    effect automatically once enough approvals come in; see
    determine_approval_tier for how many and from whom."""
    data, error = get_json_or_error()
    if error:
        return error

    reason = data.get("reason")
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    if id == g.staff["id"]:
        return jsonify({"error": "Cannot request your own deactivation"}), 400

    conn = get_db_connection()
    target = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if target is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if target["role"] != "admin":
        conn.close()
        return (
            jsonify(
                {
                    "error": "This route is only for deactivating admin accounts. Use /staff/<id>/delete for other roles."
                }
            ),
            400,
        )

    if not target["is_active"]:
        conn.close()
        return jsonify({"error": "Staff account is already deactivated"}), 400

    existing_pending = conn.execute(
        "SELECT id FROM admin_deactivation_requests WHERE target_staff_id = ? AND status = 'pending'",
        (id,),
    ).fetchone()

    if existing_pending is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "A pending deactivation request already exists for this admin",
                    "request_id": existing_pending["id"],
                }
            ),
            409,
        )

    tier, required_approvals = determine_approval_tier(conn, g.staff["id"], id)

    if tier is None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "No eligible approvers currently exist to process this request",
                    "reason": "Deactivating this admin requires approval from another admin, or at least 2 in_charge accounts, or at least 1 tool_keeper — none are currently available.",
                }
            ),
            409,
        )

    try:
        cursor = conn.execute(
            """
            INSERT INTO admin_deactivation_requests
                (target_staff_id, requested_by_staff_id, reason, required_tier, required_approvals)
            VALUES (?, ?, ?, ?, ?)
            """,
            (id, g.staff["id"], reason, tier, required_approvals),
        )
        conn.commit()
        request_id = cursor.lastrowid
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return jsonify({"error": "A database constraint was violated."}), 400
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="requested_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=request_id,
        details=(
            f"Requested deactivation of admin {target['username']}: {reason} "
            f"(requires {required_approvals} {tier} approval(s))"
        ),
    )

    return (
        jsonify(
            {
                "message": "Deactivation request submitted",
                "id": request_id,
                "required_tier": tier,
                "required_approvals": required_approvals,
            }
        ),
        201,
    )


@staff_bp.route("/admin-deactivation-requests/<int:id>/approve", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def approve_admin_deactivation(id):
    conn = get_db_connection()
    req = conn.execute(
        "SELECT * FROM admin_deactivation_requests WHERE id = ?", (id,)
    ).fetchone()

    if req is None:
        conn.close()
        return jsonify({"error": "Request not found"}), 404

    if req["status"] != "pending":
        conn.close()
        return jsonify({"error": f"This request has already been {req['status']}"}), 409

    if g.staff["role"] != req["required_tier"]:
        conn.close()
        return (
            jsonify(
                {
                    "error": f"This request requires approval from {req['required_tier']} accounts"
                }
            ),
            403,
        )

    if g.staff["id"] in (req["target_staff_id"], req["requested_by_staff_id"]):
        conn.close()
        return (
            jsonify(
                {"error": "You cannot approve a request you filed or are the target of"}
            ),
            403,
        )

    try:
        conn.execute(
            "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id) VALUES (?, ?)",
            (id, g.staff["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "You have already approved this request"}), 400

    log_action(
        staff_id=g.staff["id"],
        action="approved_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=id,
        details=f"Approved deactivation request #{id} for staff {req['target_staff_id']}",
    )

    approval_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_deactivation_approvals WHERE request_id = ?",
        (id,),
    ).fetchone()["cnt"]

    finalized = finalize_if_ready(req, conn)
    conn.close()

    if finalized:
        return jsonify(
            {
                "message": "Approval recorded — required approvals reached, admin account deactivated"
            }
        )

    return jsonify(
        {
            "message": "Approval recorded",
            "approvals_received": approval_count,
            "approvals_required": req["required_approvals"],
        }
    )


@staff_bp.route("/admin-deactivation-requests/<int:id>/deny", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def deny_admin_deactivation(id):
    """A single denial from anyone eligible to approve this request's tier
    cancels it outright — denial doesn't need consensus the way approval
    does. The requester can also cancel their own pending request."""
    data, error = get_json_or_error()
    denial_reason = data.get("denial_reason") if not error else None

    conn = get_db_connection()
    req = conn.execute(
        "SELECT * FROM admin_deactivation_requests WHERE id = ?", (id,)
    ).fetchone()

    if req is None:
        conn.close()
        return jsonify({"error": "Request not found"}), 404

    if req["status"] != "pending":
        conn.close()
        return jsonify({"error": f"This request has already been {req['status']}"}), 409

    is_requester = g.staff["id"] == req["requested_by_staff_id"]
    is_eligible_denier = g.staff["role"] == req["required_tier"] and g.staff[
        "id"
    ] not in (req["target_staff_id"], req["requested_by_staff_id"])

    if not (is_requester or is_eligible_denier):
        conn.close()
        return (
            jsonify(
                {
                    "error": "You are not authorized to deny this request",
                    "reason": f"Only the requester (to cancel it) or a {req['required_tier']} account (other than the requester or target) can deny this request",
                }
            ),
            403,
        )

    new_status = "cancelled" if is_requester else "denied"

    conn.execute(
        """
        UPDATE admin_deactivation_requests
        SET status = ?, denied_by_staff_id = ?, denial_reason = ?, resolved_at = datetime('now', 'localtime')
        WHERE id = ?
        """,
        (new_status, g.staff["id"], denial_reason, id),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action=(
            "cancelled_admin_deactivation"
            if is_requester
            else "denied_admin_deactivation"
        ),
        target_table="admin_deactivation_requests",
        target_id=id,
        details=f"Request #{id} {new_status}"
        + (f": {denial_reason}" if denial_reason else ""),
    )

    return jsonify({"message": f"Request {new_status}"})


@staff_bp.route("/admin-deactivation-requests")
@login_required
def list_admin_deactivation_requests():
    """admin/in_charge see every request — this is supervisory info.
    tool_keeper can never be a requester or target here (only admins can
    be either), so the only requests relevant to them are ones where
    their tier is the one that gets to approve/deny — everything else is
    unrelated admin-on-admin business they don't need visibility into."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    valid_statuses = ("pending", "approved", "denied", "cancelled")
    status_filter = request.args.get("status")
    if status_filter and status_filter not in valid_statuses:
        return (
            jsonify({"error": f"status must be one of: {', '.join(valid_statuses)}"}),
            400,
        )

    where_clauses = []
    params = []

    if g.staff["role"] not in ("admin", "in_charge"):
        where_clauses.append("admin_deactivation_requests.required_tier = ?")
        params.append(g.staff["role"])

    if status_filter:
        where_clauses.append("admin_deactivation_requests.status = ?")
        params.append(status_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM admin_deactivation_requests {where_sql}", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT admin_deactivation_requests.*,
               target.username AS target_username,
               requester.username AS requested_by_username,
               denier.username AS denied_by_username
        FROM admin_deactivation_requests
        JOIN staff AS target ON admin_deactivation_requests.target_staff_id = target.id
        JOIN staff AS requester ON admin_deactivation_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS denier ON admin_deactivation_requests.denied_by_staff_id = denier.id
        {where_sql}
        ORDER BY admin_deactivation_requests.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in rows],
        }
    )
