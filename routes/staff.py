from flask import Blueprint, jsonify, g, request, render_template, make_response, url_for
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.uploads import validate_image_upload, save_staff_photo, delete_staff_photo
from services.admin_deactivation_service import (
    determine_approval_tier,
    finalize_if_ready,
    finalize_promotion_if_ready,
)
from utils.logger import log_action
from utils.validators import (
    validate_date_string,
    validate_date_fields,
    validate_password_strength,
    validate_rfid_pin,
    parse_pagination_params,
)
from werkzeug.security import generate_password_hash, check_password_hash
from utils.exports import sanitize_export_filename, default_export_dir, resolve_export_dir
from services.settings_service import (
    get_session_idle_minutes,
    get_session_absolute_hours,
    get_max_failed_attempts,
    get_lockout_minutes,
    get_due_soon_hours,
    INDEFINITE_LOCK_UNTIL,
    FEATURE_FLAGS,
    get_feature_flags,
    get_default_loan_days,
    get_damage_restriction_threshold,
)
import sqlite3
import csv
import io
import os
import secrets
from datetime import datetime, timedelta
from extensions import limiter
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import logging

logger = logging.getLogger(__name__)

staff_bp = Blueprint("staff", __name__)
VALID_ROLES = ("admin", "in_charge", "tool_keeper")
# Sanity cap on a manual lock's custom duration -- 1 year. Long enough that
# nobody legitimately needs more (an indefinite lock covers that case
# directly), short enough to catch a fat-fingered value like "500000"
# minutes before it silently becomes a de facto indefinite lock that
# doesn't say so.
MAX_MANUAL_LOCK_MINUTES = 525600


# # # LIST / VIEW ------------------------------------------------------------

# "name" maps to two columns since there's no single stored full-name field
# -- same convention as members.SORTABLE_COLUMNS.
SORTABLE_STAFF_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "username": "staff.username",
    "staff_code": "staff.staff_code",
    "role": "staff.role",
    "department": "departments.name",
}


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

    sort_by = args.get("sort_by", "name")
    if sort_by not in SORTABLE_STAFF_COLUMNS:
        return None, (
            jsonify(
                {"error": f"sort_by must be one of: {', '.join(SORTABLE_STAFF_COLUMNS)}"}
            ),
            400,
        )
    sort_dir = args.get("sort_dir", "asc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)

    dir_sql = sort_dir.upper()
    order_by_sql = ", ".join(
        f"{col.strip()} {dir_sql}" for col in SORTABLE_STAFF_COLUMNS[sort_by].split(",")
    )

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
               staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        {where_clause}
        ORDER BY {order_by_sql}, staff.id {dir_sql}
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
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


@staff_bp.route("/staff")
@roles_required("admin")
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
               staff.photo_path,
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


@staff_bp.route("/staff/by-barcode/<string:barcode>")
@login_required
def staff_by_barcode(barcode):
    """Exact-match barcode lookup for the Staff search box's Enter-key
    shortcut (staff.html) -- see assets.py:asset_by_barcode for the full
    reasoning, identical here. Looks up via the linked member's barcode
    (staff has no barcode of its own -- see routes/members.py), matching
    regardless of active/locked state, same as the other two."""
    conn = get_db_connection()
    staff_row = conn.execute(
        """
        SELECT staff.id
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE members.barcode_id = ?
        """,
        (barcode,),
    ).fetchone()
    conn.close()
    if staff_row is None:
        return render_template(
            "_barcode_not_found_modal.html", kind="staff member", barcode=barcode
        )
    return staff_profile(staff_row["id"])


@staff_bp.route("/staff/<int:id>/profile")
@login_required
def staff_profile(id):
    """Renders the read-only profile modal opened by clicking a staff
    table row -- mirrors members_bp.member_profile: staff details plus a
    shell that the modal's own JS fills in by calling the existing
    /api/v1/audit-log?staff_id=<id> endpoint for an Activity tab. That
    endpoint only actually filters by staff_id for admin callers (see
    get_audit_log_context above) -- non-admins get their own entries back
    regardless of staff_id, which would look like the wrong person's
    activity, so the Activity tab is admin-only here and the template
    just omits it for everyone else."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.member_id, staff.username, staff.staff_code, staff.role,
               staff.assigned_start, staff.assigned_end, staff.is_active, staff.locked_until,
               staff.created_at, staff.photo_path, staff.rfid_id,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()

    # Same idea as members_bp.member_profile's pending_unrestriction_request:
    # surfaces an already-pending deactivation request against this admin
    # right on their profile, rather than only being discoverable via
    # Approvals -- the create endpoint already 409s on a duplicate, this
    # just makes what it already knows visible.
    pending_deactivation_request = None
    if staff_member is not None and staff_member["role"] == "admin" and staff_member["is_active"]:
        pending_deactivation_request = conn.execute(
            """
            SELECT admin_deactivation_requests.id, admin_deactivation_requests.reason,
                   admin_deactivation_requests.created_at, admin_deactivation_requests.required_tier,
                   admin_deactivation_requests.required_approvals,
                   staff.username AS requested_by_username,
                   (SELECT COUNT(*) FROM admin_deactivation_approvals
                    WHERE admin_deactivation_approvals.request_id = admin_deactivation_requests.id
                   ) AS approvals_received
            FROM admin_deactivation_requests
            JOIN staff ON admin_deactivation_requests.requested_by_staff_id = staff.id
            WHERE admin_deactivation_requests.target_staff_id = ? AND admin_deactivation_requests.status = 'pending'
            """,
            (id,),
        ).fetchone()
    conn.close()

    if staff_member is None:
        return jsonify({"error": "Staff not found"}), 404

    return render_template(
        "staff/_profile_modal.html",
        staff_member=staff_member,
        pending_deactivation_request=pending_deactivation_request,
    )


def get_audit_log_context(args, staff):
    """Shared query logic for the audit trail -- used by both the JSON
    API route below and the full /audit-log page (routes/pages.py), the
    same get_*_table_context pattern used by assets/members/staff/
    transactions/notifications. admin can view everyone's entries and
    filter by any staff_id. in_charge and tool_keeper can only view their
    own -- any staff_id they pass in is ignored and forced to their own
    id. Other filters are all optional and combine with AND. Pagination
    uses the shared limit/offset helper (default 25, capped at 100 --
    same as every other paginated route), not a route-specific limit.
    Returns (context_dict, None) on success, or (None, error_response)."""
    filters = []
    params = []

    if staff["role"] == "admin":
        staff_id = args.get("staff_id")
        if staff_id:
            filters.append("audit_log.staff_id = ?")
            params.append(staff_id)
    else:
        filters.append("audit_log.staff_id = ?")
        params.append(staff["id"])

    action = args.get("action")
    if action:
        filters.append("audit_log.action = ?")
        params.append(action)

    target_table = args.get("target_table")
    if target_table:
        filters.append("audit_log.target_table = ?")
        params.append(target_table)

    since = args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, error
        filters.append("audit_log.created_at >= ?")
        params.append(since)

    until = args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, error
        # created_at is a full timestamp, but `until` is a bare date, so a
        # plain <= comparison as strings excludes the entire day named by
        # `until` (e.g. "2026-08-06 14:32:00" <= "2026-08-06" is false).
        # Compare against the start of the *next* day instead so the named
        # day is fully included.
        until_exclusive = (
            datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1)
        ).strftime("%Y-%m-%d")
        filters.append("audit_log.created_at < ?")
        params.append(until_exclusive)

    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

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

    return {
        "logs": logs,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }, None


@staff_bp.route("/audit-log")
@login_required
def view_audit_log():
    context, error = get_audit_log_context(request.args, g.staff)
    if error:
        return error

    return jsonify(
        {
            "total_count": context["total_count"],
            "limit": context["limit"],
            "offset": context["offset"],
            "results": [dict(row) for row in context["logs"]],
        }
    )


@staff_bp.route("/audit-log/table")
@login_required
def view_audit_log_table():
    """htmx partial -- returns just the table + pagination footer, so
    filtering/paging never re-renders the whole page (matching the
    assets/members/staff/transactions/notifications pattern)."""
    context, error = get_audit_log_context(request.args, g.staff)
    if error:
        return error
    return render_template("audit_log/_table.html", **context)


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
    returns nothing rather than the whole table.

    The "Browse all" button in the modal hits this same route with
    browse=1 instead of typing a query -- same eligibility filter, no
    text match, alphabetical ordering since it's meant to be skimmed
    rather than typed against, BROWSE_PAGE_SIZE per page with a
    "Load more" row appending the next page (append=1&offset=N) rather
    than dumping the whole eligible pool into the DOM at once.

    has_more is detected by fetching one extra row past the page size
    rather than a separate COUNT(*) query -- if that extra row comes
    back, there's more to load."""
    BROWSE_PAGE_SIZE = 50

    q = request.args.get("q", "").strip()
    browse_all = request.args.get("browse") == "1"
    append = request.args.get("append") == "1"

    if not q and not browse_all:
        return render_template(
            "staff/_eligible_member_results.html", results=[], q=q, browse_all=False, has_more=False,
            loaded_so_far=0, next_offset=0,
        )

    conn = get_db_connection()

    if browse_all:
        try:
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            offset = 0

        rows = conn.execute(
            """
            SELECT members.id, members.first_name, members.last_name, members.middle_name,
                   members.suffix, members.member_code, members.barcode_id,
                   departments.name AS department
            FROM members
            JOIN departments ON members.department_id = departments.id
            WHERE members.is_active = 1
              AND members.id NOT IN (SELECT member_id FROM staff)
            ORDER BY members.last_name, members.first_name
            LIMIT ? OFFSET ?
            """,
            (BROWSE_PAGE_SIZE + 1, offset),
        ).fetchall()

        has_more = len(rows) > BROWSE_PAGE_SIZE
        results = rows[:BROWSE_PAGE_SIZE]
        loaded_so_far = offset + len(results)
        next_offset = offset + BROWSE_PAGE_SIZE

        conn.close()

        template = "staff/_eligible_member_rows_append.html" if append else "staff/_eligible_member_results.html"
        return render_template(
            template, results=results, q=q, browse_all=True, has_more=has_more,
            loaded_so_far=loaded_so_far, next_offset=next_offset,
        )

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

    return render_template(
        "staff/_eligible_member_results.html", results=results, q=q, browse_all=False, has_more=False,
        loaded_so_far=len(results), next_offset=0,
    )



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
        generated_code = f"STF{new_staff_id:05d}"
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
        target_name=data["username"],
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
               staff.staff_code, staff.is_active, staff.locked_until, staff.rfid_id,
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

    # A blank/omitted rfid_pin leaves whatever's already set untouched --
    # unlike rfid_id (a plain identifier, safe to blank out to unassign),
    # this is a login credential, so an accidental empty submit shouldn't
    # silently wipe a working PIN. Validated up front, before the row is
    # even fetched, same as the role check above.
    if data.get("rfid_pin"):
        error = validate_rfid_pin(data["rfid_pin"])
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

    new_rfid_pin_hash = (
        generate_password_hash(data["rfid_pin"])
        if data.get("rfid_pin")
        else staff["rfid_pin_hash"]
    )

    try:
        conn.execute(
            """
            UPDATE staff
            SET role = ?, assigned_start = ?, assigned_end = ?, rfid_id = ?, rfid_pin_hash = ?
            WHERE id = ?
            """,
            (
                # data.get("role", staff["role"]),
                new_role,
                data.get("assigned_start", staff["assigned_start"]),
                data.get("assigned_end", staff["assigned_end"]),
                # An empty string means "clear the card" (unassign), not
                # "set it to the literal empty string" -- normalized to
                # NULL so the UNIQUE index (idx_staff_rfid_id) still
                # treats every unassigned staff account as distinct
                # instead of colliding on "".
                data.get("rfid_id", staff["rfid_id"]) or None,
                new_rfid_pin_hash,
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
                    "error": "A database constraint was violated. Check that the RFID card isn't already assigned to another staff account."
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
        target_name=staff["username"],
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
        target_name=g.staff["username"],
        details=f"Staff {g.staff['username']} changed their own password",
    )

    return jsonify({"message": "Password changed successfully"})


@staff_bp.route("/staff/change-rfid-pin", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute; 30 per hour")
def change_rfid_pin_submit():
    """Self-service RFID PIN change -- mirrors change_password_submit
    above exactly (current password required as confirmation, not the
    current PIN itself -- a password is the stronger, unrelated secret,
    so this doesn't degrade into "prove you know the PIN to change the
    PIN"). Admin-only, matching RFID login itself (routes/auth.py:
    login_rfid) being admin-only -- there's no point letting a
    non-admin set a PIN that can never actually log anyone in.

    This is also the intended answer to "should a newly-issued RFID PIN
    be a fixed default like 000000, or random?" -- neither. Whoever
    assigns the card (an admin, via staff/_edit_modal.html) types a real
    starting PIN themselves, exactly like a new staff account already
    gets a real starting password typed by whoever creates it (see
    add_staff_submit's TEMPORARY PASSWORD field) rather than a
    predictable system default -- and this endpoint is what lets the
    new admin change it to something only they know once they've logged
    in, the same way changing an initial password already works."""
    data, error = get_json_or_error()
    if error:
        return error

    current_password = data.get("current_password")
    new_pin = data.get("new_pin")

    if not current_password or not new_pin:
        return (
            jsonify({"error": "current_password and new_pin are required"}),
            400,
        )

    if not check_password_hash(g.staff["password_hash"], current_password):
        return jsonify({"error": "Current password is incorrect"}), 401

    error = validate_rfid_pin(new_pin)
    if error:
        return error

    new_hash = generate_password_hash(new_pin)
    current_token = request.cookies.get("session_token")

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE staff SET rfid_pin_hash = ? WHERE id = ?",
            (new_hash, g.staff["id"]),
        )
        # Same reasoning as change_password_submit -- a PIN change should
        # invalidate every other active session on this account (someone
        # else could be mid-session on a shared kiosk with the old PIN
        # still valid there otherwise), but not the one making this
        # request.
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
        action="changed_own_rfid_pin",
        target_table="staff",
        target_id=g.staff["id"],
        target_name=g.staff["username"],
        details=f"Staff {g.staff['username']} changed their own RFID PIN",
    )

    return jsonify({"message": "RFID PIN changed successfully"})


# # # NOTIFICATION PREFERENCES --------------------------------------------
# Personal display prefs, not the admin-wide due_soon_hours threshold
# above (that controls WHEN an alert is generated at all; this controls
# whether *this* staff member sees it once generated) -- see
# 0022_staff_notification_prefs.sql and services/notification_service.py's
# PREF_FILTER_SQL.

_NOTIFY_PREF_COLUMNS = ("notify_due_soon", "notify_overdue", "notify_requests")


@staff_bp.route("/staff/notification-prefs", methods=["POST"])
@login_required
def update_own_notification_prefs():
    """Started as the three boolean notify_* toggles (_NOTIFY_PREF_COLUMNS
    above); also handles toast_duration_seconds here now -- a personal
    display preference (templates/base.html's toast auto-dismiss timer),
    not a permission, so it belongs in this "my own account settings"
    endpoint rather than an admin-only System page one."""
    data, error = get_json_or_error()
    if error:
        return error

    updates = {}
    for col in _NOTIFY_PREF_COLUMNS:
        if col in data:
            if not isinstance(data[col], bool):
                return jsonify({"error": f"{col} must be true or false"}), 400
            updates[col] = 1 if data[col] else 0

    if "toast_duration_seconds" in data:
        try:
            seconds = int(data["toast_duration_seconds"])
        except (TypeError, ValueError):
            return jsonify({"error": "toast_duration_seconds must be a whole number"}), 400
        if not (2 <= seconds <= 15):
            return jsonify({"error": "toast_duration_seconds must be between 2 and 15"}), 400
        updates["toast_duration_seconds"] = seconds

    if not updates:
        return jsonify({"error": "No recognized preference fields provided"}), 400

    conn = get_db_connection()
    try:
        set_sql = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(
            f"UPDATE staff SET {set_sql} WHERE id = ?",
            (*updates.values(), g.staff["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_own_notification_prefs",
        target_table="staff",
        target_id=g.staff["id"],
        target_name=g.staff["username"],
        details="Updated own notification preferences: "
        + ", ".join(
            f"{col}={val}" if col == "toast_duration_seconds" else f"{col}={'on' if val else 'off'}"
            for col, val in updates.items()
        ),
    )

    return jsonify({"message": "Notification preferences updated", **updates})


# # # PROFILE PHOTO -----------------------------------------------------


@staff_bp.route("/staff/photo", methods=["POST"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def upload_own_photo():
    """Self-service profile photo upload/replace, for the camera button on
    the My Account modal. Mirrors the asset image replace flow in
    routes/assets.py (edit_asset): validate before touching anything,
    save the new file, update the DB, and only delete the old file once
    that update has actually committed -- so a failure partway through
    never leaves the staff record pointing at a file that's already
    gone."""
    photo_file = request.files.get("photo") if request.files else None
    img_error = validate_image_upload(photo_file)
    if img_error:
        return jsonify({"error": img_error}), 400

    saved_path, img_error = save_staff_photo(photo_file, g.staff["username"])
    if img_error:
        return jsonify({"error": img_error}), 400

    old_photo_path = g.staff["photo_path"]

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE staff SET photo_path = ? WHERE id = ?",
            (saved_path, g.staff["id"]),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        # The DB update never landed, so the just-saved file would
        # otherwise be an orphan nothing ever points to or cleans up.
        delete_staff_photo(saved_path)
        raise
    finally:
        conn.close()

    delete_staff_photo(old_photo_path)

    log_action(
        staff_id=g.staff["id"],
        action="updated_own_photo",
        target_table="staff",
        target_id=g.staff["id"],
        target_name=g.staff["username"],
        details=f"Staff {g.staff['username']} updated their profile photo",
    )

    return jsonify(
        {
            "message": "Profile photo updated successfully",
            "photo_path": saved_path,
            "photo_url": url_for("static", filename=saved_path),
        }
    )


@staff_bp.route("/staff/photo", methods=["DELETE"])
@login_required
@limiter.limit("10 per minute; 30 per hour")
def remove_own_photo():
    """Self-service profile photo removal, for the "Remove" link on the My
    Account modal. Reverts the avatar back to initials. No-ops (still 200)
    if there was no photo to begin with, since the end state the caller
    wants is the same either way."""
    old_photo_path = g.staff["photo_path"]
    if not old_photo_path:
        return jsonify({"message": "No profile photo to remove", "photo_url": None})

    conn = get_db_connection()
    try:
        conn.execute(
            "UPDATE staff SET photo_path = NULL WHERE id = ?",
            (g.staff["id"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    delete_staff_photo(old_photo_path)

    log_action(
        staff_id=g.staff["id"],
        action="removed_own_photo",
        target_table="staff",
        target_id=g.staff["id"],
        target_name=g.staff["username"],
        details=f"Staff {g.staff['username']} removed their profile photo",
    )

    return jsonify({"message": "Profile photo removed successfully", "photo_url": None})


@staff_bp.route("/staff/<int:id>/reset-password-form")
@roles_required("admin")
def reset_password_form(id):
    """Renders the confirm-before-reset modal. Mirrors lock_staff_form's
    shape -- fetch the row, 404 if missing, hand off to the partial. The
    actual reset (and the generated password it hands back) happens in
    reset_password_submit below; this route only exists to get a
    confirmation in front of it, same as every other destructive-ish
    staff action in this file."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.username, staff.is_active,
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

    return render_template("staff/_reset_password_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/reset-password", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute; 30 per hour")
def reset_password_submit(id):
    """Admin-initiated reset for a staff member who forgot their password
    -- no current password needed, since that's the whole point of this
    being the recovery path. Takes no input: rather than trust an admin
    to type a strong password on someone else's behalf (or reuse a weak
    one out of habit), this generates a random one the same way
    services/seed_service.py already does for the emergency temp-admin
    account, and hands it back in the response -- once. It isn't stored
    anywhere in plaintext and this endpoint can't be asked to reveal it
    again; if it's lost before reaching the staff member, reset again.

    Also clears any lockout alongside the password, since a staff member
    who forgot their password very plausibly also got themselves locked
    out trying to guess it -- a fresh temporary password would otherwise
    be handed to someone who still can't log in with it. Every existing
    session for the account is killed either way, since a reset usually
    means the account may have been locked out or compromised."""
    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if not staff["is_active"]:
        conn.close()
        return jsonify({"error": "This account is deactivated"}), 400

    temporary_password = secrets.token_urlsafe(12)
    new_hash = generate_password_hash(temporary_password)

    try:
        conn.execute(
            """
            UPDATE staff
            SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL
            WHERE id = ?
            """,
            (new_hash, id),
        )
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
        target_name=staff["username"],
        details=f"Admin reset password for staff account: {staff['username']} (temporary password issued)",
    )

    return jsonify(
        {
            "message": "Password reset successfully",
            "temporary_password": temporary_password,
        }
    )


@staff_bp.route("/staff/<int:id>/lock-form")
@roles_required("admin")
def lock_staff_form(id):
    """Renders the manual-lock modal -- duration presets plus a custom
    minutes field and an indefinite option. Mirrors delete_staff_form's
    shape (fetch the row, 404 if missing, then hand off to the partial)."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.locked_until,
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

    if id == g.staff["id"]:
        return jsonify({"error": "You can't lock your own account"}), 403

    if not staff_member["is_active"]:
        return jsonify({"error": "This account is already deactivated"}), 400

    return render_template("staff/_lock_modal.html", staff_member=staff_member)


@staff_bp.route("/staff/<int:id>/lock", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def lock_staff(id):
    """Manually locks an account -- direct admin action, independent of
    the failed-attempts auto-lockout in routes/auth.py. Writes the same
    staff.locked_until column that auto-lockout does (an indefinite lock
    uses the INDEFINITE_LOCK_UNTIL sentinel -- see that constant's
    docstring in services/settings_service.py), so the existing Unlock
    action and the login-time lockout check both already handle it with
    no changes needed there beyond the one message tweak in auth.py.

    Deliberately doesn't touch failed_login_attempts -- that counter
    tracks a different thing (consecutive bad passwords) that a manual
    lock has nothing to do with, so it's left exactly as it was."""
    data, error = get_json_or_error()
    if error:
        return error

    if id == g.staff["id"]:
        return jsonify({"error": "You can't lock your own account"}), 403

    indefinite = bool(data.get("indefinite"))
    duration_minutes = data.get("duration_minutes")

    if not indefinite:
        try:
            duration_minutes = int(duration_minutes)
        except (TypeError, ValueError):
            return jsonify({"error": "duration_minutes must be a whole number of minutes"}), 400
        if duration_minutes <= 0:
            return jsonify({"error": "duration_minutes must be greater than 0"}), 400
        if duration_minutes > MAX_MANUAL_LOCK_MINUTES:
            return (
                jsonify(
                    {
                        "error": f"duration_minutes can't exceed {MAX_MANUAL_LOCK_MINUTES} (1 year) -- use an indefinite lock instead"
                    }
                ),
                400,
            )

    conn = get_db_connection()
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if staff is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if not staff["is_active"]:
        conn.close()
        return jsonify({"error": "This account is already deactivated"}), 400

    locked_until = (
        INDEFINITE_LOCK_UNTIL
        if indefinite
        else (datetime.now() + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    )

    conn.execute(
        "UPDATE staff SET locked_until = ? WHERE id = ?",
        (locked_until, id),
    )
    # A lock is meant to actively cut the account off, not just block new
    # logins later -- so any session already signed in gets dropped too,
    # same reasoning as the password-reset/password-change flows above.
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (id,))
    conn.commit()
    conn.close()

    detail = (
        f"Manually locked staff account (indefinite): {staff['username']}"
        if indefinite
        else f"Manually locked staff account for {duration_minutes} minute(s): {staff['username']}"
    )
    log_action(
        staff_id=g.staff["id"],
        action="locked_staff",
        target_table="staff",
        target_id=id,
        target_name=staff["username"],
        details=detail,
    )

    return jsonify({"message": "Staff account locked successfully", "locked_until": locked_until})


@staff_bp.route("/staff/<int:id>/unlock-form")
@roles_required("admin")
def unlock_staff_form(id):
    """Renders the confirm-before-unlock modal -- previously this action
    fired straight off a plain browser confirm() (see staffAction() in
    staff.html), inconsistent with every other state-changing action in
    this table (deactivate, lock, etc.) which all get a proper modal.
    Mirrors lock_staff_form's shape."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.locked_until, staff.failed_login_attempts,
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

    if not staff_member["locked_until"] and staff_member["failed_login_attempts"] == 0:
        return jsonify({"error": "This account is not currently locked"}), 400

    return render_template("staff/_unlock_modal.html", staff_member=staff_member)


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
        target_name=staff["username"],
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
        target_name=staff["username"],
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
        target_name=staff["username"],
        details=f"Reactivated staff account: {staff['username']}",
    )

    return jsonify({"message": "Staff reactivated successfully"})


@staff_bp.route("/staff/bulk-deactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_deactivate_staff():
    """Deactivates multiple staff accounts in one request. Mirrors
    bulk_deactivate_members's shape (deactivated/skipped, one bad row
    doesn't fail the batch) and delete_staff's guards: admin accounts are
    skipped rather than deactivated here, since those only ever leave
    active status through the peer-approval request-deactivation flow,
    not a direct action -- same as the single-row Actions dropdown, which
    swaps "Deactivate" for "Request Deactivation" on admin rows instead of
    wiring the direct action up at all."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return (
            jsonify({"error": "Missing required field: ids (must be a non-empty list)"}),
            400,
        )

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    deactivated, skipped = [], []

    for staff_id in ids:
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

        if row is None:
            skipped.append({"id": staff_id, "reason": "Staff not found"})
            continue

        if row["role"] == "admin":
            skipped.append(
                {
                    "id": staff_id,
                    "name": row["username"],
                    "reason": "Admin accounts require a deactivation request instead",
                }
            )
            continue

        if not row["is_active"]:
            skipped.append({"id": staff_id, "name": row["username"], "reason": "Already deactivated"})
            continue

        try:
            conn.execute(
                "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL WHERE id = ?",
                (staff_id,),
            )
            conn.commit()
            deactivated.append({"id": staff_id, "name": row["username"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk deactivate of staff {staff_id}: {e}")
            skipped.append(
                {"id": staff_id, "name": row["username"], "reason": "A database constraint was violated"}
            )

    conn.close()

    if deactivated:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_deactivated_staff",
            target_table="staff",
            details=(
                f"Bulk deactivated {len(deactivated)} staff account(s): "
                + ", ".join(s["name"] for s in deactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return jsonify(
        {
            "deactivated_count": len(deactivated),
            "deactivated": deactivated,
            "skipped": skipped,
        }
    )


@staff_bp.route("/staff/bulk-reactivate", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def bulk_reactivate_staff():
    """Reactivates multiple staff accounts in one request. Sibling of
    bulk_deactivate_staff above -- see that docstring for the shared
    shape. No admin-role guard needed here: reactivating never routes
    through peer approval the way deactivating an admin does."""
    data, error = get_json_or_error()
    if error:
        return error

    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return (
            jsonify({"error": "Missing required field: ids (must be a non-empty list)"}),
            400,
        )

    try:
        ids = [int(i) for i in raw_ids]
    except (ValueError, TypeError):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    reactivated, skipped = [], []

    for staff_id in ids:
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

        if row is None:
            skipped.append({"id": staff_id, "reason": "Staff not found"})
            continue

        if row["is_active"]:
            skipped.append({"id": staff_id, "name": row["username"], "reason": "Already active"})
            continue

        try:
            conn.execute("UPDATE staff SET is_active = 1 WHERE id = ?", (staff_id,))
            conn.commit()
            reactivated.append({"id": staff_id, "name": row["username"]})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk reactivate of staff {staff_id}: {e}")
            skipped.append(
                {"id": staff_id, "name": row["username"], "reason": "A database constraint was violated"}
            )

    conn.close()

    if reactivated:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_reactivated_staff",
            target_table="staff",
            details=(
                f"Bulk reactivated {len(reactivated)} staff account(s): "
                + ", ".join(s["name"] for s in reactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return jsonify(
        {
            "reactivated_count": len(reactivated),
            "reactivated": reactivated,
            "skipped": skipped,
        }
    )


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
        target_name=target["username"],
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

    # req is a plain SELECT * from admin_deactivation_requests, no join --
    # target_username isn't a column on it, has to be looked up separately.
    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()
    log_action(
        staff_id=g.staff["id"],
        action="approved_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=id,
        target_name=target_staff_row["username"] if target_staff_row else None,
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

    # Looked up before conn.close() below -- req itself (a plain SELECT *
    # from admin_deactivation_requests) has no target_username column.
    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()

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
        target_name=target_staff_row["username"] if target_staff_row else None,
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
    elif request.args.get("mine") == "1":
        # Same opt-in narrowing as list_unrestriction_requests's `mine`
        # param -- admin/in_charge see everyone's by default, this lets
        # them filter down to requests they personally filed.
        where_clauses.append("admin_deactivation_requests.requested_by_staff_id = ?")
        params.append(g.staff["id"])

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
               denier.username AS denied_by_username,
               (SELECT COUNT(*) FROM admin_deactivation_approvals
                WHERE admin_deactivation_approvals.request_id = admin_deactivation_requests.id
               ) AS approvals_received
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

    context = {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "requests_list": rows,
    }

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "requests/_deactivation_table.html", staff=g.staff, **context
        )

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in rows],
        }
    )


# # # ADMIN PROMOTION (peer-approved hand-over) requests ---------------------
# Structurally mirrors the admin-deactivation routes just above --
# request/approve/deny/list, same tier/eligibility rules -- but for the
# opposite direction: nominating a non-admin staff member to become an
# admin, instead of removing one. See services/admin_deactivation_service.py's
# finalize_promotion_if_ready and migrations/0036_admin_promotion_requests.sql.


@staff_bp.route("/staff/<int:id>/request-promotion", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def request_admin_promotion(id):
    """Nominates a non-admin staff member for promotion to admin -- takes
    effect automatically once enough peer approvals come in, the same
    quorum shape as request_admin_deactivation, so one admin can't
    unilaterally hand the role to an ally."""
    data, error = get_json_or_error()
    if error:
        return error

    reason = data.get("reason")
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    conn = get_db_connection()
    target = conn.execute("SELECT * FROM staff WHERE id = ?", (id,)).fetchone()

    if target is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if target["role"] == "admin":
        conn.close()
        return jsonify({"error": "This staff member is already an admin"}), 400

    if not target["is_active"]:
        conn.close()
        return jsonify({"error": "Cannot promote a deactivated staff account"}), 400

    existing_pending = conn.execute(
        "SELECT id FROM admin_promotion_requests WHERE target_staff_id = ? AND status = 'pending'",
        (id,),
    ).fetchone()

    if existing_pending is not None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "A pending promotion request already exists for this staff member",
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
                    "reason": "Promoting this staff member requires approval from another admin, or at least 2 in_charge accounts, or at least 1 tool_keeper — none are currently available.",
                }
            ),
            409,
        )

    try:
        cursor = conn.execute(
            """
            INSERT INTO admin_promotion_requests
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
        action="requested_admin_promotion",
        target_table="admin_promotion_requests",
        target_id=request_id,
        target_name=target["username"],
        details=(
            f"Requested promotion of {target['username']} to admin: {reason} "
            f"(requires {required_approvals} {tier} approval(s))"
        ),
    )

    return (
        jsonify(
            {
                "message": "Promotion request submitted",
                "id": request_id,
                "required_tier": tier,
                "required_approvals": required_approvals,
            }
        ),
        201,
    )


@staff_bp.route("/admin-promotion-requests/<int:id>/approve", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def approve_admin_promotion(id):
    conn = get_db_connection()
    req = conn.execute(
        "SELECT * FROM admin_promotion_requests WHERE id = ?", (id,)
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
            "INSERT INTO admin_promotion_approvals (request_id, approver_staff_id) VALUES (?, ?)",
            (id, g.staff["id"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "You have already approved this request"}), 400

    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()
    log_action(
        staff_id=g.staff["id"],
        action="approved_admin_promotion",
        target_table="admin_promotion_requests",
        target_id=id,
        target_name=target_staff_row["username"] if target_staff_row else None,
        details=f"Approved promotion request #{id} for staff {req['target_staff_id']}",
    )

    approval_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_promotion_approvals WHERE request_id = ?",
        (id,),
    ).fetchone()["cnt"]

    finalized = finalize_promotion_if_ready(req, conn)
    conn.close()

    if finalized:
        return jsonify(
            {
                "message": "Approval recorded — required approvals reached, staff member promoted to admin"
            }
        )

    return jsonify(
        {
            "message": "Approval recorded",
            "approvals_received": approval_count,
            "approvals_required": req["required_approvals"],
        }
    )


@staff_bp.route("/admin-promotion-requests/<int:id>/deny", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def deny_admin_promotion(id):
    """A single denial from anyone eligible to approve this request's tier
    cancels it outright -- same as admin-deactivation's deny. The
    requester can also cancel their own pending request."""
    data, error = get_json_or_error()
    denial_reason = data.get("denial_reason") if not error else None

    conn = get_db_connection()
    req = conn.execute(
        "SELECT * FROM admin_promotion_requests WHERE id = ?", (id,)
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

    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()

    conn.execute(
        """
        UPDATE admin_promotion_requests
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
            "cancelled_admin_promotion"
            if is_requester
            else "denied_admin_promotion"
        ),
        target_name=target_staff_row["username"] if target_staff_row else None,
        target_table="admin_promotion_requests",
        target_id=id,
        details=f"Request #{id} {new_status}"
        + (f": {denial_reason}" if denial_reason else ""),
    )

    return jsonify({"message": f"Request {new_status}"})


@staff_bp.route("/admin-promotion-requests")
@login_required
def list_admin_promotion_requests():
    """Same visibility rules as list_admin_deactivation_requests: admin/
    in_charge see every request (supervisory view), everyone else only
    sees requests whose required_tier matches their own role."""
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
        where_clauses.append("admin_promotion_requests.required_tier = ?")
        params.append(g.staff["role"])
    elif request.args.get("mine") == "1":
        where_clauses.append("admin_promotion_requests.requested_by_staff_id = ?")
        params.append(g.staff["id"])

    if status_filter:
        where_clauses.append("admin_promotion_requests.status = ?")
        params.append(status_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM admin_promotion_requests {where_sql}", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT admin_promotion_requests.*,
               target.username AS target_username,
               requester.username AS requested_by_username,
               denier.username AS denied_by_username,
               (SELECT COUNT(*) FROM admin_promotion_approvals
                WHERE admin_promotion_approvals.request_id = admin_promotion_requests.id
               ) AS approvals_received
        FROM admin_promotion_requests
        JOIN staff AS target ON admin_promotion_requests.target_staff_id = target.id
        JOIN staff AS requester ON admin_promotion_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS denier ON admin_promotion_requests.denied_by_staff_id = denier.id
        {where_sql}
        ORDER BY admin_promotion_requests.created_at DESC
        LIMIT ? OFFSET ?
        """,
        params + [limit, offset],
    ).fetchall()
    conn.close()

    context = {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "requests_list": rows,
    }

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "requests/_promotion_table.html", staff=g.staff, **context
        )

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "results": [dict(row) for row in rows],
        }
    )


# # # LOAN LIMITS (per-member-type concurrent-loan caps) ----------------------


@staff_bp.route("/loan-limits/manage-form")
@roles_required("admin")
def manage_loan_limits_form():
    conn = get_db_connection()
    limits = conn.execute(
        "SELECT * FROM loan_limits ORDER BY member_type"
    ).fetchall()
    conn.close()
    return render_template("staff/_loan_limits_modal.html", limits=limits, error=None)


@staff_bp.route("/loan-limits/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_loan_limits():
    """Updates the admin-editable per-member-type concurrent-loan caps that
    routes/transactions.py's get_max_concurrent_loans() reads at checkout
    time. Both member types are submitted and validated together from the
    one modal, rather than one row at a time."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    updates = {}
    for member_type in ("trainee", "trainer"):
        raw = data.get(f"max_concurrent_loans_{member_type}")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            error_msg = f"Loan limit for {member_type} must be a whole number"
            if request.is_json:
                return jsonify({"error": error_msg}), 400
            conn = get_db_connection()
            limits = conn.execute("SELECT * FROM loan_limits ORDER BY member_type").fetchall()
            conn.close()
            return render_template(
                "staff/_loan_limits_modal.html", limits=limits, error=error_msg
            ), 200
        if value < 1:
            error_msg = f"Loan limit for {member_type} must be at least 1"
            if request.is_json:
                return jsonify({"error": error_msg}), 400
            conn = get_db_connection()
            limits = conn.execute("SELECT * FROM loan_limits ORDER BY member_type").fetchall()
            conn.close()
            return render_template(
                "staff/_loan_limits_modal.html", limits=limits, error=error_msg
            ), 200
        updates[member_type] = value

    conn = get_db_connection()
    for member_type, value in updates.items():
        conn.execute(
            "UPDATE loan_limits SET max_concurrent_loans = ? WHERE member_type = ?",
            (value, member_type),
        )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_loan_limits",
        target_table="loan_limits",
        details=f"Set concurrent-loan limits: trainee={updates['trainee']}, trainer={updates['trainer']}",
    )

    if request.is_json:
        return jsonify({"message": "Loan limits updated successfully", **updates})
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # BULK IMPORT (CSV) ------------------------------------------------------


REQUIRED_STAFF_CSV_COLUMNS = ("member_identifier", "username", "password", "role")
OPTIONAL_STAFF_CSV_COLUMNS = ("assigned_start", "assigned_end")


@staff_bp.route("/staff/import-form")
@roles_required("admin")
def import_staff_form():
    return render_template("staff/_import_modal.html", error=None)


def _staff_import_error_response(error_msg, status):
    """Mirrors assets.py's _import_error_response -- see that function's
    docstring for why HX-Retarget/HX-Reswap are needed."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(
        render_template("staff/_import_modal.html", error=error_msg), 200
    )
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@staff_bp.route("/staff/import", methods=["POST"])
@roles_required("admin")
@limiter.limit("5 per minute")
def import_staff_csv():
    """Bulk-creates staff accounts from a CSV, mirroring
    routes/assets.py's import_assets_csv (preview/commit dry-run pattern,
    same manual transaction control). Unlike assets/members, a staff
    account can't be created standalone -- each row must reference an
    *existing* member (by member_code or barcode_id, via the
    member_identifier column) who isn't already staff, exactly like the
    single Add Staff form requires picking from that same candidate list."""
    if "file" not in request.files:
        return _staff_import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _staff_import_error_response("No file selected", 400)

    mode = request.form.get("mode", "commit")
    if mode not in ("preview", "commit"):
        return _staff_import_error_response("mode must be 'preview' or 'commit'", 400)
    dry_run = mode == "preview"

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _staff_import_error_response("File must be UTF-8 encoded CSV", 400)

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []
    header_map = {h: (h or "").strip().lower().replace(" ", "_") for h in raw_fieldnames}
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_STAFF_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return _staff_import_error_response(
            f"CSV missing required column(s): {', '.join(missing_cols)}", 400
        )

    conn = get_db_connection()
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors = [], []

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        member_identifier = (row.get("member_identifier") or "").strip()
        username = (row.get("username") or "").strip()
        password = row.get("password") or ""
        role = (row.get("role") or "").strip().lower()

        if not member_identifier or not username or not password or not role:
            errors.append(
                {
                    "row": row_num,
                    "error": "member_identifier, username, password, and role are required",
                }
            )
            continue

        if role not in VALID_ROLES:
            errors.append(
                {"row": row_num, "error": f"role must be one of: {', '.join(VALID_ROLES)}"}
            )
            continue

        if len(password) < 8:
            errors.append(
                {"row": row_num, "error": "password must be at least 8 characters"}
            )
            continue

        member = conn.execute(
            """
            SELECT id FROM members
            WHERE member_code = ? OR barcode_id = ?
            """,
            (member_identifier, member_identifier),
        ).fetchone()
        if member is None:
            errors.append(
                {
                    "row": row_num,
                    "error": f"No member found matching member_identifier '{member_identifier}'",
                }
            )
            continue

        conn.execute("SAVEPOINT row_import")
        try:
            hashed_password = generate_password_hash(password)
            cursor = conn.execute(
                """
                INSERT INTO staff (member_id, username, password_hash, role, assigned_start, assigned_end, is_active, created_by_staff_id)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    member["id"],
                    username,
                    hashed_password,
                    role,
                    row.get("assigned_start") or None,
                    row.get("assigned_end") or None,
                    g.staff["id"],
                ),
            )
            new_id = cursor.lastrowid
            conn.execute(
                "UPDATE staff SET staff_code = ? WHERE id = ?",
                (f"STF{new_id:05d}", new_id),
            )
            conn.execute("RELEASE row_import")
            created.append({"row": row_num, "id": new_id, "username": username})
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append(
                {"row": row_num, "error": f"duplicate or constraint violation: {e}"}
            )

    if dry_run:
        conn.execute("ROLLBACK")
    else:
        conn.execute("COMMIT")
    conn.close()

    if dry_run:
        return render_template(
            "staff/_import_preview.html",
            rows_to_create=len(created),
            errors=errors,
        )

    log_action(
        staff_id=g.staff["id"],
        action="bulk_imported_staff",
        target_table="staff",
        details=f"Imported {len(created)} staff account(s) via CSV, {len(errors)} error(s)",
    )

    if request.is_json:
        return jsonify(
            {"created_count": len(created), "created": created, "errors": errors}
        ), (201 if created else 400)

    response = make_response(
        render_template(
            "staff/_import_results.html",
            created_count=len(created),
            created=created,
            errors=errors,
        )
    )
    if created:
        response.headers["HX-Trigger"] = "staff-refreshed"
    return response


# # # EXPORT --------------------------------------------------------------

# Deliberately excludes password_hash, locked_until, and
# failed_login_attempts -- credential/security-internal fields that have
# no business ever landing in a spreadsheet, unlike everything else here
# which is already visible in the Staff table itself.
STAFF_EXPORT_COLUMNS = (
    "staff_code",
    "username",
    "role",
    "first_name",
    "last_name",
    "middle_name",
    "suffix",
    "barcode_id",
    "department",
    "assigned_start",
    "assigned_end",
    "is_active",
    "created_at",
)

STAFF_EXPORT_FIELD_LABELS = {
    "staff_code": "Staff Code",
    "username": "Username",
    "role": "Role",
    "first_name": "First Name",
    "last_name": "Last Name",
    "middle_name": "Middle Name",
    "suffix": "Suffix",
    "barcode_id": "Barcode ID",
    "department": "Department",
    "assigned_start": "Assigned Start",
    "assigned_end": "Assigned End",
    "is_active": "Active",
    "created_at": "Created At",
}


def _default_staff_export_basename(show_inactive):
    return (
        f"staff_{'inactive' if show_inactive else 'active'}_"
        f"{datetime.now().strftime('%Y%m%d')}"
    )


@staff_bp.route("/staff/export-form")
@roles_required("admin")
def export_staff_form():
    """admin-only -- matches every other staff-page mutation/export-ish
    action on this page (Add Staff, Import CSV, Manage Loan Limits are
    all admin-only here too), and the Staff tab itself (GET /staff,
    both the page and the underlying list API) is admin-only as well."""
    show_inactive = request.args.get("show_inactive") == "1"
    return render_template(
        "staff/_export_modal.html",
        show_inactive=show_inactive,
        default_filename=_default_staff_export_basename(show_inactive),
        default_export_dir=default_export_dir(),
        fields=[(col, STAFF_EXPORT_FIELD_LABELS[col]) for col in STAFF_EXPORT_COLUMNS],
    )


@staff_bp.route("/staff/export", methods=["GET", "POST"])
@roles_required("admin")
def export_staff():
    """Mirrors routes/assets.py's export_assets/routes/members.py's
    export_members -- see export_staff_form's docstring for why this one
    is admin-only rather than just login_required."""
    show_inactive = request.values.get("show_inactive") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    selected = request.values.getlist("fields")
    if selected:
        columns = [c for c in STAFF_EXPORT_COLUMNS if c in selected]
        if not columns:
            columns = list(STAFF_EXPORT_COLUMNS)
    else:
        columns = list(STAFF_EXPORT_COLUMNS)

    conn = get_db_connection()
    where_clause = "staff.is_active = 0" if show_inactive else "staff.is_active = 1"
    staff_rows = conn.execute(
        f"""
        SELECT staff.staff_code, staff.username, staff.role,
               staff.assigned_start, staff.assigned_end, staff.is_active, staff.created_at,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               members.barcode_id, departments.name AS department
        FROM staff
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE {where_clause}
        ORDER BY staff.id
        """
    ).fetchall()
    conn.close()

    basename = sanitize_export_filename(
        request.values.get("filename"), _default_staff_export_basename(show_inactive)
    )

    if export_format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Inactive Staff" if show_inactive else "Staff"

        header_font = Font(bold=True)
        ws.append([STAFF_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for cell in ws[1]:
            cell.font = header_font

        for row in staff_rows:
            ws.append([row[col] if row[col] is not None else "" for col in columns])

        for i, col in enumerate(columns, start=1):
            longest = max(
                [len(col)] + [len(str(row[col] or "")) for row in staff_rows]
            )
            ws.column_dimensions[get_column_letter(i)].width = min(
                max(longest + 2, 10), 40
            )
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        file_bytes = buf.getvalue()
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        export_format = "csv"
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([STAFF_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for row in staff_rows:
            writer.writerow([row[col] if row[col] is not None else "" for col in columns])
        file_bytes = output.getvalue().encode("utf-8-sig")
        content_type = "text/csv"

    filename = f"{basename}.{export_format}"

    response = make_response(file_bytes)
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"

    if request.method == "POST":
        save_dir, was_clamped = resolve_export_dir(request.values.get("save_path"))
        try:
            os.makedirs(save_dir, exist_ok=True)
            saved_path = os.path.join(save_dir, filename)
            with open(saved_path, "wb") as f:
                f.write(file_bytes)
            response.headers["X-Export-Saved-Path"] = saved_path
            if was_clamped:
                response.headers["X-Export-Warning"] = (
                    "The folder entered was outside the app directory, "
                    "so the export was saved to the default export folder instead."
                )
        except OSError as e:
            logger.warning(f"Could not save export copy to disk: {e}")
            response.headers["X-Export-Warning"] = (
                "Could not save a copy on the server; your download still completed."
            )

    return response


# # # SESSION SETTINGS (admin-editable login-session timeouts) --------------


@staff_bp.route("/session-settings/manage-form")
@roles_required("admin")
def manage_session_settings_form():
    return render_template(
        "staff/_session_settings_modal.html",
        session_idle_minutes=get_session_idle_minutes(),
        session_absolute_hours=get_session_absolute_hours(),
        error=None,
    )


@staff_bp.route("/session-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_session_settings():
    """Updates the two admin-editable session-timeout values that
    services/auth_service.py's require_session() and routes/auth.py's
    login() read at runtime (see services/settings_service.py) -- how
    long a session can sit idle before "Session timed out" kicks in, and
    the absolute ceiling on a session's lifetime regardless of activity.
    Both submitted and validated together from the one modal, same
    pattern as update_loan_limits."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg):
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_session_settings_modal.html",
                session_idle_minutes=get_session_idle_minutes(),
                session_absolute_hours=get_session_absolute_hours(),
                error=error_msg,
            ),
            200,
        )

    try:
        idle_minutes = int(data.get("session_idle_minutes"))
    except (TypeError, ValueError):
        return _rerender_with_error("Idle timeout must be a whole number of minutes")
    if not (1 <= idle_minutes <= 1440):
        return _rerender_with_error("Idle timeout must be between 1 and 1440 minutes (24 hours)")

    try:
        absolute_hours = int(data.get("session_absolute_hours"))
    except (TypeError, ValueError):
        return _rerender_with_error("Absolute session length must be a whole number of hours")
    if not (1 <= absolute_hours <= 720):
        return _rerender_with_error("Absolute session length must be between 1 and 720 hours (30 days)")

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'session_idle_minutes'",
        (str(idle_minutes),),
    )
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'session_absolute_hours'",
        (str(absolute_hours),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_session_settings",
        target_table="app_settings",
        details=f"Set session_idle_minutes={idle_minutes}, session_absolute_hours={absolute_hours}",
    )

    if request.is_json:
        return jsonify(
            {
                "message": "Session settings updated successfully",
                "session_idle_minutes": idle_minutes,
                "session_absolute_hours": absolute_hours,
            }
        )
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # ACCOUNT SECURITY SETTINGS (admin-editable lockout policy) --------------


@staff_bp.route("/account-security/manage-form")
@roles_required("admin")
def manage_account_security_form():
    return render_template(
        "staff/_account_security_modal.html",
        max_failed_attempts=get_max_failed_attempts(),
        lockout_minutes=get_lockout_minutes(),
        error=None,
    )


@staff_bp.route("/account-security/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_account_security():
    """Updates the two admin-editable lockout-policy values that
    routes/auth.py's login() reads at runtime -- how many failed login
    attempts trigger a lockout, and how long that lockout lasts."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg):
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_account_security_modal.html",
                max_failed_attempts=get_max_failed_attempts(),
                lockout_minutes=get_lockout_minutes(),
                error=error_msg,
            ),
            200,
        )

    try:
        max_attempts = int(data.get("max_failed_attempts"))
    except (TypeError, ValueError):
        return _rerender_with_error("Max failed attempts must be a whole number")
    if not (1 <= max_attempts <= 20):
        return _rerender_with_error("Max failed attempts must be between 1 and 20")

    try:
        lockout_minutes = int(data.get("lockout_minutes"))
    except (TypeError, ValueError):
        return _rerender_with_error("Lockout duration must be a whole number of minutes")
    if not (1 <= lockout_minutes <= 1440):
        return _rerender_with_error("Lockout duration must be between 1 and 1440 minutes (24 hours)")

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'max_failed_attempts'",
        (str(max_attempts),),
    )
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'lockout_minutes'",
        (str(lockout_minutes),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_account_security_settings",
        target_table="app_settings",
        details=f"Set max_failed_attempts={max_attempts}, lockout_minutes={lockout_minutes}",
    )

    if request.is_json:
        return jsonify(
            {
                "message": "Account security settings updated successfully",
                "max_failed_attempts": max_attempts,
                "lockout_minutes": lockout_minutes,
            }
        )
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # NOTIFICATION SETTINGS (admin-editable due-soon threshold) -------------


@staff_bp.route("/notification-settings/manage-form")
@roles_required("admin")
def manage_notification_settings_form():
    return render_template(
        "staff/_notification_settings_modal.html",
        due_soon_hours=get_due_soon_hours(),
        error=None,
    )


@staff_bp.route("/notification-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_notification_settings():
    """Updates the admin-editable due-soon threshold that
    services/notification_service.py's sync_loan_notifications() reads
    at runtime -- how many hours before a loan's due date a 'due_soon'
    alert fires."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg):
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_notification_settings_modal.html",
                due_soon_hours=get_due_soon_hours(),
                error=error_msg,
            ),
            200,
        )

    try:
        due_soon_hours = int(data.get("due_soon_hours"))
    except (TypeError, ValueError):
        return _rerender_with_error("Due-soon threshold must be a whole number of hours")
    if not (1 <= due_soon_hours <= 168):
        return _rerender_with_error("Due-soon threshold must be between 1 and 168 hours (1 week)")

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'due_soon_hours'",
        (str(due_soon_hours),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_notification_settings",
        target_table="app_settings",
        details=f"Set due_soon_hours={due_soon_hours}",
    )

    if request.is_json:
        return jsonify(
            {
                "message": "Notification settings updated successfully",
                "due_soon_hours": due_soon_hours,
            }
        )
    # No HX-Redirect here, unlike the other System-page settings modals
    # (session-settings/account-security/loan-limits) -- those are only
    # ever opened from the System page itself, so redirecting there on
    # save is a no-op-feeling refresh. This modal is also opened from the
    # notification bell dropdown's gear icon on every page (see
    # templates/base.html), where forcing a navigation to /system would
    # yank an admin off whatever page they were actually on just to save a
    # threshold. An empty body swapped into #global-modal-container simply
    # clears/closes the modal in place instead -- see the htmx:afterRequest
    # listener in base.html for the confirmation toast.
    return make_response("", 200)


# # # LOAN DURATION (admin-editable default checkout due date) --------------


@staff_bp.route("/loan-duration/manage-form")
@roles_required("admin")
def manage_loan_duration_form():
    return render_template(
        "staff/_loan_duration_modal.html",
        default_loan_days=get_default_loan_days(),
        error=None,
    )


@staff_bp.route("/loan-duration/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_loan_duration():
    """Updates the admin-editable fallback due-date length that
    routes/transactions.py's checkout_session() reads when a checkout is
    submitted with no due date explicitly picked."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg):
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_loan_duration_modal.html",
                default_loan_days=get_default_loan_days(),
                error=error_msg,
            ),
            200,
        )

    try:
        default_loan_days = int(data.get("default_loan_days"))
    except (TypeError, ValueError):
        return _rerender_with_error("Default loan duration must be a whole number of days")
    if not (1 <= default_loan_days <= 90):
        return _rerender_with_error("Default loan duration must be between 1 and 90 days")

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'default_loan_days'",
        (str(default_loan_days),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_loan_duration",
        target_table="app_settings",
        details=f"Set default_loan_days={default_loan_days}",
    )

    if request.is_json:
        return jsonify(
            {
                "message": "Loan duration updated successfully",
                "default_loan_days": default_loan_days,
            }
        )
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # DAMAGE RESTRICTION (admin-editable auto-restrict occurrence threshold) --


@staff_bp.route("/damage-restriction/manage-form")
@roles_required("admin")
def manage_damage_restriction_form():
    return render_template(
        "staff/_damage_restriction_modal.html",
        damage_restriction_threshold=get_damage_restriction_threshold(),
        error=None,
    )


@staff_bp.route("/damage-restriction/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_damage_restriction():
    """Updates the admin-editable occurrence threshold that
    services/loan_service.py's check_and_restrict_damaged_returns() reads
    at return time -- how many damaged-condition returns a member can
    have on record before their account is auto-restricted."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg):
        if request.is_json:
            return jsonify({"error": error_msg}), 400
        return (
            render_template(
                "staff/_damage_restriction_modal.html",
                damage_restriction_threshold=get_damage_restriction_threshold(),
                error=error_msg,
            ),
            200,
        )

    try:
        threshold = int(data.get("damage_restriction_threshold"))
    except (TypeError, ValueError):
        return _rerender_with_error("Damage restriction threshold must be a whole number")
    if not (1 <= threshold <= 50):
        return _rerender_with_error("Damage restriction threshold must be between 1 and 50")

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'damage_restriction_threshold'",
        (str(threshold),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_damage_restriction_threshold",
        target_table="app_settings",
        details=f"Set damage_restriction_threshold={threshold}",
    )

    if request.is_json:
        return jsonify(
            {
                "message": "Damage restriction threshold updated successfully",
                "damage_restriction_threshold": threshold,
            }
        )
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # FEATURE ACCESS (admin-editable tab/workflow toggles) --------------------


@staff_bp.route("/feature-flags/manage-form")
@roles_required("admin")
def manage_feature_flags_form():
    return render_template(
        "staff/_feature_flags_modal.html",
        feature_flags=get_feature_flags(),
        error=None,
    )


@staff_bp.route("/feature-flags/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_feature_flags():
    """Flips one or more of the toggles on the System page's Feature
    Access card (see migrations/0033_feature_flags.sql,
    services/settings_service.py's FEATURE_FLAGS/get_feature_flags,
    services/auth_service.py's feature_required). Requires the admin's
    current password, same check_password_hash pattern change_password_
    submit/change_rfid_pin_submit already use for other sensitive
    self-service actions -- required going either direction (enabling or
    disabling) since both are a real access-control change worth
    re-confirming, not just disabling."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    def _rerender_with_error(error_msg, status=400):
        if request.is_json:
            return jsonify({"error": error_msg}), status
        return (
            render_template(
                "staff/_feature_flags_modal.html",
                feature_flags=get_feature_flags(),
                error=error_msg,
            ),
            200,
        )

    current_password = (data.get("current_password") or "").strip()
    if not current_password:
        return _rerender_with_error("Enter your current password to confirm this change")
    if not check_password_hash(g.staff["password_hash"], current_password):
        return _rerender_with_error("Current password is incorrect", status=401)

    # Checkbox fields are only present in form data when checked -- an
    # unchecked toggle simply doesn't show up in `data` at all, unlike a
    # JSON body where the caller can explicitly send `false`. Handle both:
    # a JSON caller sends {"flags": {"assets": true, ...}}; the htmx form
    # sends flag_<name> only for the checked ones.
    if request.is_json:
        submitted = data.get("flags") or {}
        new_values = {name: bool(submitted.get(name, current)) for name, current in get_feature_flags().items()}
    else:
        new_values = {name: (f"flag_{name}" in data) for name in FEATURE_FLAGS}

    conn = get_db_connection()
    current_flags = get_feature_flags(conn)
    changed = {name: val for name, val in new_values.items() if val != current_flags[name]}
    for name, enabled in changed.items():
        key, _label = FEATURE_FLAGS[name]
        conn.execute(
            "UPDATE app_settings SET value = ? WHERE key = ?",
            ("1" if enabled else "0", key),
        )
    conn.commit()
    conn.close()

    for name, enabled in changed.items():
        _key, label = FEATURE_FLAGS[name]
        log_action(
            staff_id=g.staff["id"],
            action="enabled_feature" if enabled else "disabled_feature",
            target_table="app_settings",
            target_name=label,
        )

    if request.is_json:
        return jsonify({"message": "Feature access updated", "feature_flags": new_values})
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response
