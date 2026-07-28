from flask import Blueprint, jsonify, g, request
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


@staff_bp.route("/staff")
@login_required
def view_staff():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    q = request.args.get("q", "").strip()
    role = request.args.get("role")
    department_id = request.args.get("department_id")
    show_inactive = request.args.get("show_inactive") == "1"

    if role and role not in VALID_ROLES:
        return (
            jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}),
            400,
        )

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
    staff = conn.execute(
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
