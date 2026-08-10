from flask import Blueprint, jsonify, g, request, render_template, make_response, url_for, send_file
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from utils.uploads import validate_image_upload, save_staff_photo, delete_staff_photo
from services.admin_deactivation_service import (
    determine_approval_tier,
    finalize_admin_deactivation,
    is_eligible_reviewer,
    get_review_counts,
)
from services.notification_service import create_admin_deactivation_requested_notifications
from utils.formatters import full_name
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
    PERMANENT_ASSIGNED_END,
    FEATURE_FLAGS,
    get_feature_flags,
    get_default_loan_days,
    get_damage_restriction_threshold,
    get_auto_backup_enabled,
    get_auto_backup_interval_hours,
    get_archive_purge_days,
)
from services.backup_service import get_last_backup_at, run_backup_now
from services.archive_purge_service import (
    purge_expired_archived_assets,
    purge_expired_inactive_staff,
    purge_expired_inactive_members,
)
import backup_db
import re
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
# Cap on a manual lock's duration -- 1 year, so a fat-fingered value like "500000" minutes doesn't quietly become an indefinite lock.
MAX_MANUAL_LOCK_MINUTES = 525600


# # # LIST / VIEW ------------------------------------------------------------

# "name" maps to two columns since there's no single stored full-name field, same as members.SORTABLE_COLUMNS.
SORTABLE_STAFF_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "username": "staff.username",
    "staff_code": "staff.staff_code",
    "role": "staff.role",
    "department": "departments.name",
}

# Days before expiry the USAGE column flips to "Expiring Soon". Fixed (unlike get_due_soon_hours) since assignment windows run in months.
USAGE_EXPIRING_SOON_DAYS = 7


# Accepts "YYYY-MM-DD" with or without a time component; returns None on bad values instead of raising.
_ASSIGNED_END_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")


def _parse_assigned_end(value):
    for fmt in _ASSIGNED_END_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def compute_usage_status(role, assigned_end):
    """Derives the staff table's USAGE badge and remaining-days count from a staff
    account's assignment window.

    "admin" is always "permanent" (login() never checks admin assigned_start/end).
    in_charge is treated like tool_keeper -- both can have a real assignment window,
    so in_charge shows whatever's on the row instead of a forced "Indefinite". Note
    login() itself only *enforces* the window for tool_keeper, so an expired in_charge
    can still log in -- this function only affects the display, not that gap.

    Returns (None, None) if no assigned_end is set (already shown as "Not set"
    elsewhere). Otherwise (status, days_left): status is "permanent"/"active"/
    "expiring_soon"/"expired"; days_left is None for permanent, else the day count
    (negative if expired) that the template turns into "X days left" / "Expired Xd ago"."""
    if role == "admin":
        return "permanent", None
    if not assigned_end:
        return None, None
    if assigned_end == PERMANENT_ASSIGNED_END:
        return "permanent", None
    end_date = _parse_assigned_end(assigned_end)
    if end_date is None:
        return None, None
    days_left = (end_date - datetime.now().date()).days
    if days_left < 0:
        return "expired", days_left
    if days_left <= USAGE_EXPIRING_SOON_DAYS:
        return "expiring_soon", days_left
    return "active", days_left


def get_staff_table_context(args):
    """Shared query logic for the staff table, used by both the htmx partial route and
    the full-page /staff route, so the two can't drift out of sync on filters.
    Returns (context_dict, error_response)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    q = args.get("q", "").strip()
    role = args.get("role")
    department_id = args.get("department_id")
    show_inactive = args.get("show_inactive") == "1"
    # `status` is the fine-grained stat-card filter (active/locked/inactive), separate from the show_inactive toggle.
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

    # `status`, when given, takes precedence over the plain show_inactive check below.
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
        # show_inactive filters to *only* inactive rows -- don't turn this into "show all",
        # that used to hide reactivations and made the filter look broken.
        where = ["staff.is_active = 0"] if show_inactive else ["staff.is_active = 1"]
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
    # Named staff_list, not "staff", to avoid colliding with the logged-in-user `staff` template var.
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

    # Convert to plain dicts so we can bolt on usage_status (it's derived, not a real column).
    staff_list = [dict(row) for row in staff_list]
    for row in staff_list:
        row["usage_status"], row["usage_days_left"] = compute_usage_status(
            row["role"], row["assigned_end"]
        )

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
        # staff=g.staff needed here too -- _table.html's can_manage check reads it on every htmx swap.
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
    """Exact-match barcode lookup for the Staff search box's Enter key (see
    assets.py:asset_by_barcode for the same pattern). Looks up via the linked
    member's barcode since staff has none of its own; matches regardless of
    active/locked state."""
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
    """Read-only profile modal for a staff table row (mirrors member_profile).
    The Activity tab is admin-only: /api/v1/audit-log only filters by staff_id for
    admin callers, so a non-admin would see their own entries mislabeled as the
    profiled staffer's activity -- the template just omits the tab for everyone else."""
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

    # Same idea as member_profile's pending_unrestriction_request -- surfaces a pending deactivation request on the profile itself.
    pending_deactivation_request = None
    if staff_member is not None and staff_member["role"] == "admin" and staff_member["is_active"]:
        pending_deactivation_request = conn.execute(
            """
            SELECT admin_deactivation_requests.id, admin_deactivation_requests.reason,
                   admin_deactivation_requests.created_at, admin_deactivation_requests.required_tier,
                   admin_deactivation_requests.required_approvals,
                   admin_deactivation_requests.required_in_charge,
                   staff.username AS requested_by_username,
                   (SELECT COUNT(*) FROM admin_deactivation_approvals
                    WHERE admin_deactivation_approvals.request_id = admin_deactivation_requests.id AND decision = 'approve'
                   ) AS approvals_received,
                   (SELECT COUNT(*) FROM admin_deactivation_approvals aa
                    JOIN staff s ON s.id = aa.approver_staff_id
                    WHERE aa.request_id = admin_deactivation_requests.id AND aa.decision = 'approve' AND s.role = 'in_charge'
                   ) AS in_charge_approvals_received
            FROM admin_deactivation_requests
            JOIN staff ON admin_deactivation_requests.requested_by_staff_id = staff.id
            WHERE admin_deactivation_requests.target_staff_id = ? AND admin_deactivation_requests.status = 'pending'
            """,
            (id,),
        ).fetchone()
    conn.close()

    if staff_member is None:
        return jsonify({"error": "Staff not found"}), 404

    usage_status, usage_days_left = compute_usage_status(
        staff_member["role"], staff_member["assigned_end"]
    )

    return render_template(
        "staff/_profile_modal.html",
        staff_member=staff_member,
        pending_deactivation_request=pending_deactivation_request,
        usage_status=usage_status,
        usage_days_left=usage_days_left,
    )


@staff_bp.route("/staff/<int:id>/full-activity-modal")
@roles_required("admin")
def staff_full_activity_modal(id):
    """Paginated "Full Activity" popup for the profile modal's Activity tab, stacking
    on top of the modal instead of navigating away to /audit-log. admin-only, same
    reason as the Activity tab (get_audit_log_context only scopes by staff_id for admins)."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()
    if staff_member is None:
        return jsonify({"error": "Staff not found"}), 404

    return render_template("staff/_full_activity_modal.html", staff_member=staff_member)


def get_audit_log_context(args, staff):
    """Shared query logic for the audit trail (JSON API + full page).
    admin can view everyone's entries and filter by staff_id; in_charge/tool_keeper
    are forced to their own id regardless of what they pass. Other filters are
    optional and AND together. Returns (context_dict, None) or (None, error_response)."""
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
        # `until` is a bare date but created_at is a full timestamp, so compare against the next day's start or a plain <= would exclude that whole day.
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
    """htmx partial -- just the table + pagination footer, so filtering/paging
    doesn't re-render the whole page."""
    context, error = get_audit_log_context(request.args, g.staff)
    if error:
        return error
    return render_template("audit_log/_table.html", **context)


# # # CREATE (admin only) ------------------------------------------------


@staff_bp.route("/staff/add-form")
@roles_required("admin")
def add_staff_form():
    """Renders the "Add New Staff" modal. A staff account grants access to an
    existing member record (staff.member_id is UNIQUE NOT NULL), so there's no
    name/department to collect here. Member picker is a type-ahead search
    (see eligible_members_search) instead of a preloaded dropdown, which
    doesn't scale once there are a lot of members."""
    return render_template("staff/_add_modal.html")


@staff_bp.route("/staff/eligible-members-search")
@roles_required("admin")
def eligible_members_search():
    """Backs the Add Staff modal's member picker. Matches name, member code, or
    barcode ID, restricted to active members not already staff. Capped at a
    handful of results (live-as-you-type search) -- an empty query returns
    nothing rather than the whole table.

    "Browse all" hits this same route with browse=1: same eligibility filter,
    no text match, alphabetical, paginated via append=1&offset=N ("Load more")
    instead of dumping the whole pool into the DOM.

    has_more is detected by fetching one extra row past BROWSE_PAGE_SIZE instead
    of a separate COUNT(*) query."""
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
    editable (matches edit_staff_submit); name/department/staff_code shown
    read-only for context."""
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


@staff_bp.route("/staff/<int:id>/reassign-form")
@roles_required("admin")
def reassign_usage_form(id):
    """Quick "Reassign Usage" modal, a shortcut to just extend/shorten an
    assignment window instead of opening the full Edit Staff modal. Posts
    to the same /staff/<id>/edit endpoint (untouched fields stay unchanged),
    so there's no separate write path to maintain.

    Available for in_charge too, not just tool_keeper -- only admin is blocked
    below, since compute_usage_status treats in_charge like tool_keeper for
    display and in_charge can have a real assigned window set."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role, staff.staff_code, staff.assigned_start, staff.assigned_end,
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
        return (
            jsonify({"error": "Admin accounts don't have an assignment window to reassign"}),
            400,
        )

    return render_template("staff/_reassign_modal.html", staff_member=staff_member)


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

    # Blank/omitted rfid_pin leaves the existing one untouched -- it's a credential, so an empty submit shouldn't wipe it.
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
                # Empty string means "unassign the card" -- normalize to NULL so the UNIQUE index doesn't collide multiple unassigned rows on "".
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
        # kill every other active session for this account, but keep this one so they're not logged out
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
    """Self-service RFID PIN change, mirrors change_password_submit. Confirms with
    the current password (not the current PIN) since a password is the stronger
    secret. Admin-only, matching RFID login itself being admin-only.

    New PINs start as a real value typed by whoever assigns the card (no
    predictable default like 000000) -- this endpoint is how the admin later
    changes it to something only they know."""
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
        # Same as change_password_submit: kill other sessions (a shared kiosk could still have the old PIN valid), but not this one.
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
# Personal display prefs -- distinct from the admin-wide due_soon_hours (that's WHEN an alert fires; this is whether *this* staffer sees it).

_NOTIFY_PREF_COLUMNS = ("notify_due_soon", "notify_overdue", "notify_requests")


@staff_bp.route("/staff/notification-prefs", methods=["POST"])
@login_required
def update_own_notification_prefs():
    """Handles the three boolean notify_* toggles plus toast_duration_seconds
    (the toast auto-dismiss timer) -- both personal prefs, so they live in this
    self-service endpoint rather than an admin-only settings one."""
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
    """Self-service profile photo upload/replace (My Account modal camera button).
    Mirrors the asset image replace flow: validate, save new file, update DB, only
    delete the old file after commit -- so a failure partway through never leaves
    the record pointing at a deleted file."""
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
        # DB update failed, so clean up the file we just saved before it becomes an orphan.
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
    """Confirm-before-reset modal. Mirrors lock_staff_form's shape; the actual
    reset happens in reset_password_submit below."""
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
    """Admin-initiated password reset -- no current password needed since that's
    the point. Takes no input: generates a random password (like the emergency
    temp-admin flow in seed_service.py) and returns it once, never stored in
    plaintext or retrievable again; if it's lost, just reset again.

    Also clears any lockout, since forgetting a password often means getting
    locked out trying to guess it. All existing sessions get killed too, since
    a reset usually means the account was locked out or compromised."""
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
    """Manual-lock modal -- duration presets, custom minutes field, and an
    indefinite option. Mirrors delete_staff_form's shape."""
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
    """Manually locks an account, independent of the auto-lockout in auth.py.
    Writes the same staff.locked_until column (indefinite uses INDEFINITE_LOCK_UNTIL,
    see settings_service.py), so Unlock and the login check handle it without changes.
    Deliberately leaves failed_login_attempts alone -- that counter is unrelated."""
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
    # A lock should cut the account off now, not just block future logins -- so any live session gets dropped too, same as the reset/change-password flows above.
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
            "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
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


@staff_bp.route("/staff/<int:id>/reactivate-confirm")
@roles_required("admin")
def reactivate_staff_form(id):
    """Renders the reactivate-confirmation modal -- used to skip straight
    to a native confirm() via staffAction() (templates/staff.html), which
    didn't match the styled modal-per-action shape every other staff
    action here uses (lock, unlock, delete-confirm, and now
    request-deactivation all render their own modal first)."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.is_active,
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

    if staff_member["is_active"]:
        return jsonify({"error": "Staff account is already active"}), 400

    return render_template("staff/_reactivate_modal.html", staff_member=staff_member)


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
        conn.execute("UPDATE staff SET is_active = 1, deactivated_at = NULL WHERE id = ?", (id,))
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
                "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
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
            conn.execute("UPDATE staff SET is_active = 1, deactivated_at = NULL WHERE id = ?", (staff_id,))
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


@staff_bp.route("/staff/<int:id>/request-deactivation-form")
@roles_required("admin")
def request_deactivation_form(id):
    """Renders the peer-approval deactivation-request modal -- used to be
    a plain browser prompt() asking for the reason inline, which didn't
    match the styled modal-plus-reason-textarea shape every other
    reason-requiring action in this app uses (members/_restrict_modal.html,
    the deny-request flows in requests.html). Same not-an-admin /
    already-pending guards request_admin_deactivation itself enforces are
    repeated here so a direct hit to this URL can't show a form for a
    request that would just get rejected on submit."""
    conn = get_db_connection()
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role, staff.is_active,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()

    if staff_member is None:
        conn.close()
        return jsonify({"error": "Staff not found"}), 404

    if staff_member["role"] != "admin":
        conn.close()
        return jsonify({"error": "This form is only for deactivating admin accounts"}), 400

    if not staff_member["is_active"]:
        conn.close()
        return jsonify({"error": "This account is already deactivated"}), 400

    if id == g.staff["id"]:
        conn.close()
        return jsonify({"error": "Cannot request your own deactivation"}), 400

    existing_pending = conn.execute(
        "SELECT id FROM admin_deactivation_requests WHERE target_staff_id = ? AND status = 'pending'",
        (id,),
    ).fetchone()
    conn.close()

    return render_template(
        "staff/_request_deactivation_modal.html",
        staff_member=staff_member,
        already_pending=existing_pending is not None,
    )


@staff_bp.route("/staff/<int:id>/request-deactivation", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def request_admin_deactivation(id):
    """Requests deactivation of a fellow admin account instead of doing it
    unilaterally — /staff/<id>/delete refuses this for any admin. Doesn't
    take effect on its own; see determine_approval_tier for how many
    approvals it needs, and /finalize for how it actually gets carried out."""
    data, error = get_json_or_error()
    if error:
        return error

    reason = data.get("reason")
    if not reason:
        return jsonify({"error": "Missing required field: reason"}), 400

    if id == g.staff["id"]:
        return jsonify({"error": "Cannot request your own deactivation"}), 400

    conn = get_db_connection()
    target = conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff JOIN members ON members.id = staff.member_id
        WHERE staff.id = ?
        """,
        (id,),
    ).fetchone()

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

    tier, required_approvals, required_in_charge = determine_approval_tier(conn, g.staff["id"], id)

    if tier is None:
        conn.close()
        return (
            jsonify(
                {
                    "error": "No eligible reviewers currently exist to process this request",
                    "reason": "Deactivating this admin requires at least 1 active in_charge account plus at least 1 more in_charge or tool_keeper account -- none are currently available.",
                }
            ),
            409,
        )

    target_full_name = full_name(
        target["first_name"], target["last_name"], target["middle_name"], target["suffix"]
    )
    requester_member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
        (g.staff["member_id"],),
    ).fetchone()
    requester_full_name = full_name(
        requester_member["first_name"],
        requester_member["last_name"],
        requester_member["middle_name"],
        requester_member["suffix"],
    )

    try:
        cursor = conn.execute(
            """
            INSERT INTO admin_deactivation_requests
                (target_staff_id, requested_by_staff_id, reason, required_tier, required_approvals, required_in_charge)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (id, g.staff["id"], reason, tier, required_approvals, required_in_charge),
        )
        request_id = cursor.lastrowid
        # Notifies every in_charge/tool_keeper account, same transaction as the insert so a request never exists without its notifications.
        create_admin_deactivation_requested_notifications(
            conn, request_id, target_full_name, requester_full_name
        )
        conn.commit()
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
            f"(requires {required_approvals} approval(s), {required_in_charge}+ in_charge)"
        ),
    )

    return (
        jsonify(
            {
                "message": "Deactivation request submitted",
                "id": request_id,
                "required_tier": tier,
                "required_approvals": required_approvals,
                "required_in_charge": required_in_charge,
            }
        ),
        201,
    )


@staff_bp.route("/admin-deactivation-requests/<int:id>/approve", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def approve_admin_deactivation(id):
    data, error = get_json_or_error()
    reason = data.get("reason") if not error else None
    if not reason:
        return jsonify({"error": "A reason is required to approve this request"}), 400

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

    if not is_eligible_reviewer(g.staff["role"]):
        conn.close()
        return (
            jsonify(
                {"error": "Only in_charge and tool_keeper accounts can review this request"}
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
            "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id, decision, reason) VALUES (?, ?, 'approve', ?)",
            (id, g.staff["id"], reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "You have already reviewed this request"}), 400

    # req has no join, so target_username isn't on it -- look it up separately.
    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()
    log_action(
        staff_id=g.staff["id"],
        action="approved_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=id,
        target_name=target_staff_row["username"] if target_staff_row else None,
        details=f"Approved deactivation request #{id} for staff {req['target_staff_id']}: {reason}",
    )

    # Just records the vote -- reaching the threshold doesn't deactivate anything by itself,
    # the requester finalizes explicitly after reviewing every vote (see /finalize).
    approve_count, in_charge_count, _ = get_review_counts(conn, id)
    conn.close()

    return jsonify(
        {
            "message": "Approval recorded",
            "approvals_received": approve_count,
            "approvals_required": req["required_approvals"],
            "in_charge_received": in_charge_count,
            "in_charge_required": req["required_in_charge"],
        }
    )


@staff_bp.route("/admin-deactivation-requests/<int:id>/deny", methods=["POST"])
@login_required
@limiter.limit("20 per minute")
def deny_admin_deactivation(id):
    """Reviewer denials are just a recorded vote now, same as approvals --
    they don't cancel the request by themselves, the requester decides
    what to do once they've seen everyone's votes and reasons (see
    /finalize). The requester can still cancel their own pending request
    outright at any time, regardless of vote counts -- that's the one path
    here that actually resolves the request."""
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
    is_eligible_denier = is_eligible_reviewer(g.staff["role"]) and g.staff["id"] not in (
        req["target_staff_id"],
        req["requested_by_staff_id"],
    )

    if not (is_requester or is_eligible_denier):
        conn.close()
        return (
            jsonify(
                {
                    "error": "You are not authorized to deny this request",
                    "reason": "Only the requester (to cancel it) or an in_charge/tool_keeper account (other than the requester or target) can deny this request",
                }
            ),
            403,
        )

    # A deny vote needs a reason so the requester sees why before finalizing -- a self-cancel doesn't, they already know.
    if not is_requester and not denial_reason:
        conn.close()
        return jsonify({"error": "A reason is required to deny this request"}), 400

    # Looked up before conn.close() below -- req has no target_username column of its own.
    target_staff_row = conn.execute(
        "SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()

    if is_requester:
        conn.execute(
            """
            UPDATE admin_deactivation_requests
            SET status = 'cancelled', denied_by_staff_id = ?, denial_reason = ?, resolved_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (g.staff["id"], denial_reason, id),
        )
        conn.commit()
        conn.close()
        log_action(
            staff_id=g.staff["id"],
            action="cancelled_admin_deactivation",
            target_name=target_staff_row["username"] if target_staff_row else None,
            target_table="admin_deactivation_requests",
            target_id=id,
            details=f"Request #{id} cancelled" + (f": {denial_reason}" if denial_reason else ""),
        )
        return jsonify({"message": "Request cancelled"})

    try:
        conn.execute(
            "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id, decision, reason) VALUES (?, ?, 'deny', ?)",
            (id, g.staff["id"], denial_reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return jsonify({"error": "You have already reviewed this request"}), 400

    log_action(
        staff_id=g.staff["id"],
        action="denied_admin_deactivation",
        target_name=target_staff_row["username"] if target_staff_row else None,
        target_table="admin_deactivation_requests",
        target_id=id,
        details=f"Denied deactivation request #{id}" + (f": {denial_reason}" if denial_reason else ""),
    )

    approve_count, in_charge_count, deny_count = get_review_counts(conn, id)
    conn.close()
    return jsonify({"message": "Deny recorded", "denies_received": deny_count})


@staff_bp.route("/admin-deactivation-requests/<int:id>/finalize", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def finalize_admin_deactivation_route(id):
    """The requester's own final say -- deactivates the target once the
    required approvals are in, but only when the requester themselves
    pulls the trigger. Reaching the threshold alone never does this
    automatically (see approve_admin_deactivation)."""
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

    if g.staff["id"] != req["requested_by_staff_id"]:
        conn.close()
        return (
            jsonify({"error": "Only the staff member who filed this request can finalize it"}),
            403,
        )

    ok, error_message = finalize_admin_deactivation(req, g.staff["id"], conn)
    conn.close()

    if not ok:
        return jsonify({"error": error_message}), 409

    return jsonify({"message": "Admin account deactivated"})


@staff_bp.route("/admin-deactivation-requests/<int:id>/reviews")
@roles_required("admin")
def list_admin_deactivation_reviews(id):
    """Every individual approve/deny vote on a request, with whoever cast
    it and their reason -- what the requester actually reviews before
    deciding whether to finalize. Admin-only -- in_charge/tool_keeper can
    still vote and see the aggregate counts on the main list, but not who
    voted which way or why."""
    conn = get_db_connection()
    req = conn.execute(
        "SELECT * FROM admin_deactivation_requests WHERE id = ?", (id,)
    ).fetchone()

    if req is None:
        conn.close()
        return jsonify({"error": "Request not found"}), 404

    reviews = conn.execute(
        """
        SELECT admin_deactivation_approvals.*, staff.username, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM admin_deactivation_approvals
        JOIN staff ON staff.id = admin_deactivation_approvals.approver_staff_id
        JOIN members ON members.id = staff.member_id
        WHERE admin_deactivation_approvals.request_id = ?
        ORDER BY admin_deactivation_approvals.created_at ASC
        """,
        (id,),
    ).fetchall()

    target_staff_row = conn.execute(
        "SELECT username, member_id FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()
    target_member = (
        conn.execute("SELECT * FROM members WHERE id = ?", (target_staff_row["member_id"],)).fetchone()
        if target_staff_row
        else None
    )
    approve_count, in_charge_count, deny_count = get_review_counts(conn, id)
    conn.close()

    if request.headers.get("HX-Request") == "true":
        return render_template(
            "staff/_deactivation_reviews_modal.html",
            staff=g.staff,
            req=req,
            reviews=reviews,
            target_member=target_member,
            approve_count=approve_count,
            in_charge_count=in_charge_count,
            deny_count=deny_count,
            requirements_met=(
                approve_count >= req["required_approvals"] and in_charge_count >= req["required_in_charge"]
            ),
        )

    return jsonify({"results": [dict(r) for r in reviews]})


@staff_bp.route("/admin-deactivation-requests")
@login_required
def list_admin_deactivation_requests():
    """Every role sees every request now -- admin/in_charge for supervisory
    visibility, in_charge/tool_keeper because they can vote on any of them
    regardless of tier, so there's no longer a "not relevant to you" subset
    to hide the way there used to be when a request's tier gated who could
    even see it."""
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

    if request.args.get("mine") == "1":
        # Same opt-in narrowing as list_unrestriction_requests's `mine` param.
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

    # my_decision's g.staff.id param has to go first in the list -- it's bound in the SELECT
    # clause, which comes before WHERE in the query text.
    rows = conn.execute(
        f"""
        SELECT admin_deactivation_requests.*,
               target.username AS target_username,
               target_member.first_name AS target_first_name,
               target_member.last_name AS target_last_name,
               target_member.middle_name AS target_middle_name,
               target_member.suffix AS target_suffix,
               requester.username AS requested_by_username,
               requester_member.first_name AS requested_by_first_name,
               requester_member.last_name AS requested_by_last_name,
               requester_member.middle_name AS requested_by_middle_name,
               requester_member.suffix AS requested_by_suffix,
               denier.username AS denied_by_username,
               denier_member.first_name AS denied_by_first_name,
               denier_member.last_name AS denied_by_last_name,
               denier_member.middle_name AS denied_by_middle_name,
               denier_member.suffix AS denied_by_suffix,
               (SELECT COUNT(*) FROM admin_deactivation_approvals aa
                WHERE aa.request_id = admin_deactivation_requests.id AND aa.decision = 'approve'
               ) AS approvals_received,
               (SELECT COUNT(*) FROM admin_deactivation_approvals aa
                JOIN staff s ON s.id = aa.approver_staff_id
                WHERE aa.request_id = admin_deactivation_requests.id AND aa.decision = 'approve' AND s.role = 'in_charge'
               ) AS in_charge_approvals_received,
               (SELECT COUNT(*) FROM admin_deactivation_approvals aa
                WHERE aa.request_id = admin_deactivation_requests.id AND aa.decision = 'deny'
               ) AS denies_received,
               (SELECT decision FROM admin_deactivation_approvals aa
                WHERE aa.request_id = admin_deactivation_requests.id AND aa.approver_staff_id = ?
               ) AS my_decision
        FROM admin_deactivation_requests
        JOIN staff AS target ON admin_deactivation_requests.target_staff_id = target.id
        JOIN members AS target_member ON target.member_id = target_member.id
        JOIN staff AS requester ON admin_deactivation_requests.requested_by_staff_id = requester.id
        JOIN members AS requester_member ON requester.member_id = requester_member.id
        LEFT JOIN staff AS denier ON admin_deactivation_requests.denied_by_staff_id = denier.id
        LEFT JOIN members AS denier_member ON denier.member_id = denier_member.id
        {where_sql}
        ORDER BY admin_deactivation_requests.created_at DESC
        LIMIT ? OFFSET ?
        """,
        [g.staff["id"]] + params + [limit, offset],
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


# # # LOAN LIMITS (per-member-type concurrent-loan caps) ----------------------


@staff_bp.route("/loan-limits/manage-form")
@roles_required("admin", "in_charge")
def manage_loan_limits_form():
    conn = get_db_connection()
    limits = conn.execute(
        "SELECT * FROM loan_limits ORDER BY member_type"
    ).fetchall()
    conn.close()
    return render_template("staff/_loan_limits_modal.html", limits=limits, error=None)


@staff_bp.route("/loan-limits/update", methods=["POST"])
@roles_required("admin", "in_charge")
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

# Deliberately excludes password_hash, locked_until, and failed_login_attempts -- security-internal fields that have no business in a spreadsheet.
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
@roles_required("admin", "in_charge")
def manage_notification_settings_form():
    return render_template(
        "staff/_notification_settings_modal.html",
        due_soon_hours=get_due_soon_hours(),
        error=None,
    )


@staff_bp.route("/notification-settings/update", methods=["POST"])
@roles_required("admin", "in_charge")
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
    # No HX-Redirect here, unlike the other System-page settings modals -- this one is also
    # opened from the notification bell dropdown on every page, so redirecting to /system would
    # yank the admin off whatever page they were on. Just clears the modal in place instead.
    return make_response("", 200)


# # # LOAN DURATION (admin-editable default checkout due date) --------------


@staff_bp.route("/loan-duration/manage-form")
@roles_required("admin", "in_charge")
def manage_loan_duration_form():
    return render_template(
        "staff/_loan_duration_modal.html",
        default_loan_days=get_default_loan_days(),
        error=None,
    )


@staff_bp.route("/loan-duration/update", methods=["POST"])
@roles_required("admin", "in_charge")
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
@roles_required("admin", "in_charge")
def manage_damage_restriction_form():
    return render_template(
        "staff/_damage_restriction_modal.html",
        damage_restriction_threshold=get_damage_restriction_threshold(),
        error=None,
    )


@staff_bp.route("/damage-restriction/update", methods=["POST"])
@roles_required("admin", "in_charge")
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

    # Handle both: JSON sends {"flags": {...}} with real booleans; the htmx form only sends flag_<name> when checked.
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
            details=(
                f"{label} was turned on -- its tab/routes are visible and usable again for everyone"
                if enabled
                else f"{label} was turned off -- its nav link is hidden and its routes/actions are blocked for everyone"
            ),
        )

    if request.is_json:
        return jsonify({"message": "Feature access updated", "feature_flags": new_values})
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/system"
    return response


# # # BARCODE LABEL ACTIVITY LOGGING ---------------------------------------
# Barcode Labels renders/downloads entirely client-side (JsBarcode + jsPDF), so these two
# endpoints exist purely to log an audit trail. They take the browser's already-built
# selection since the server never sees which checkboxes were ticked.


def _describe_barcode_selection(asset_items, member_items):
    """Turns the two item lists into the human-readable "N asset(s)
    (CODE - Name, ...); N member(s) (CODE - Name, ...)" string used in
    both endpoints' audit_log details -- same "count + comma-joined
    names" shape as bulk_deactivate_staff's log entry above."""
    parts = []
    if asset_items:
        parts.append(
            f"{len(asset_items)} asset(s) ("
            + ", ".join(f"{i.get('code', '?')} - {i.get('label', '?')}" for i in asset_items)
            + ")"
        )
    if member_items:
        parts.append(
            f"{len(member_items)} member(s) ("
            + ", ".join(f"{i.get('code', '?')} - {i.get('label', '?')}" for i in member_items)
            + ")"
        )
    return "; ".join(parts) if parts else "no items"


def _split_barcode_items(items):
    asset_items = [i for i in items if i.get("type") == "asset"]
    member_items = [i for i in items if i.get("type") == "member"]
    return asset_items, member_items


@staff_bp.route("/barcodes/log-generate", methods=["POST"])
@roles_required("admin", "in_charge")
def log_barcode_generate():
    """Called by barcodes.html's generate() right after it opens the
    label preview -- records which specific assets/members were picked,
    not just "someone opened the barcode page" (routes/pages.py's
    barcodes_page doesn't log anything itself; viewing the picker isn't
    an action worth an audit entry, generating a concrete label sheet
    for named items is)."""
    data, error = get_json_or_error()
    if error:
        return error

    items = data.get("items") or []
    if not items:
        return jsonify({"error": "items is required"}), 400

    asset_items, member_items = _split_barcode_items(items)
    log_action(
        staff_id=g.staff["id"],
        action="generated_barcode_labels",
        details=f"Generated a label preview for {_describe_barcode_selection(asset_items, member_items)}",
    )
    return jsonify({"logged": True})


@staff_bp.route("/barcodes/log-download", methods=["POST"])
@roles_required("admin", "in_charge")
def log_barcode_download():
    """Mirrors log_barcode_generate above, called right after
    confirmDownload() actually saves the PDF -- kept as a separate
    action/entry (not folded into the generate log) since someone can
    preview a sheet and back out without ever downloading it, and the
    two are meaningfully different events to see in the Activity Log."""
    data, error = get_json_or_error()
    if error:
        return error

    items = data.get("items") or []
    if not items:
        return jsonify({"error": "items is required"}), 400
    size = data.get("size") or "regular"
    if size not in ("small", "regular", "large"):
        size = "regular"

    asset_items, member_items = _split_barcode_items(items)
    log_action(
        staff_id=g.staff["id"],
        action="downloaded_barcode_labels",
        details=f"Downloaded a {size} PDF for {_describe_barcode_selection(asset_items, member_items)}",
    )
    return jsonify({"logged": True})


# # # ONLINE STAFF ----------------------------------------------------------


def get_online_staff(conn, roles=None):
    """Staff with a currently-live session -- the exact same "live"
    definition require_session (services/auth_service.py) uses to decide
    whether a request's own session cookie is still good: not past
    expires_at, and not idle past get_session_idle_minutes. login()
    (routes/auth.py) always deletes any existing session row for that
    staff_id before inserting a fresh one, so there's at most one session
    row per staff member -- this is just "join staff to its live session
    row", no need to dedupe multiple sessions per person.

    `roles`, if given, restricts to that subset (e.g. in_charge's view of
    this only wants tool_keeper, not other in_charge/admin -- see
    view_online_staff below)."""
    idle_minutes = get_session_idle_minutes(conn)
    where_role = ""
    params = [f"-{idle_minutes} minutes"]
    if roles:
        placeholders = ",".join("?" * len(roles))
        where_role = f"AND staff.role IN ({placeholders})"
        params.extend(roles)
    return conn.execute(
        f"""
        SELECT staff.id, staff.username, staff.role, staff.photo_path,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               departments.name AS department, sessions.last_active_at
        FROM sessions
        JOIN staff ON staff.id = sessions.staff_id
        JOIN members ON staff.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE sessions.expires_at > datetime('now', 'localtime')
          AND sessions.last_active_at >= datetime('now', 'localtime', ?)
          {where_role}
        ORDER BY sessions.last_active_at DESC
        """,
        params,
    ).fetchall()


@staff_bp.route("/staff/online")
@roles_required("admin", "in_charge")
def view_online_staff():
    """Backs the Dashboard's "Currently Online" widget
    (templates/dashboard/_online_staff.html), polled every 30s via htmx
    so it stays live without a full page refresh. admin sees everyone
    with a live session; in_charge sees both in_charge and tool_keeper --
    their own peers (other in_charge staff) are just as relevant to them
    as tool_keepers are, so it's not narrowed to tool_keeper alone."""
    conn = get_db_connection()
    roles = None if g.staff["role"] == "admin" else ("in_charge", "tool_keeper")
    online_staff = get_online_staff(conn, roles=roles)
    conn.close()
    return render_template(
        "dashboard/_online_staff.html",
        online_staff=online_staff,
        viewer_role=g.staff["role"],
        viewer_id=g.staff["id"],
    )


# # # DATABASE BACKUPS -------------------------------------------------------
# Admin-only System page card for the scheduled backup job -- toggle it, change the interval,
# run one now, and download existing files. backup_db.py stays the source of truth for
# where backups live and how many are kept.

# Matches backup_database()'s filename format -- the allowlist the download route checks
# against so it can only ever serve a real backup file, never an arbitrary path.
_BACKUP_FILENAME_RE = re.compile(r"^toolroom_\d{8}_\d{6}\.db$")


def _list_backups():
    """Newest-first list of {name, size_bytes, created_at} for every
    backup file currently on disk -- shown in the manage-form modal so
    an admin can see backup history isn't just a claim, and download any
    of them. Empty list (not an error) if the backup directory doesn't
    exist yet -- true before the very first backup has ever run."""
    backup_dir = backup_db.DEFAULT_BACKUP_DIR
    if not os.path.isdir(backup_dir):
        return []
    entries = []
    for name in os.listdir(backup_dir):
        if not _BACKUP_FILENAME_RE.match(name):
            continue
        path = os.path.join(backup_dir, name)
        stat = os.stat(path)
        entries.append(
            {
                "name": name,
                "size_bytes": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    entries.sort(key=lambda e: e["name"], reverse=True)
    return entries


@staff_bp.route("/backup-settings/manage-form")
@roles_required("admin")
def manage_backup_settings_form():
    conn = get_db_connection()
    enabled = get_auto_backup_enabled(conn)
    interval_hours = get_auto_backup_interval_hours(conn)
    last_backup_at = get_last_backup_at(conn)
    conn.close()
    return render_template(
        "staff/_backup_settings_modal.html",
        enabled=enabled,
        interval_hours=interval_hours,
        last_backup_at=last_backup_at,
        backups=_list_backups(),
        keep_last_n=backup_db.KEEP_LAST_N,
    )


@staff_bp.route("/backup-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_backup_settings():
    data, error = get_json_or_error()
    if error:
        return error

    enabled = bool(data.get("enabled"))
    try:
        interval_hours = int(data.get("interval_hours"))
    except (TypeError, ValueError):
        return jsonify({"error": "interval_hours must be a whole number"}), 400
    if interval_hours < 1 or interval_hours > 168:
        return jsonify({"error": "interval_hours must be between 1 and 168 (one week)"}), 400

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'auto_backup_enabled'",
        ("1" if enabled else "0",),
    )
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'auto_backup_interval_hours'",
        (str(interval_hours),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_backup_settings",
        target_table="app_settings",
        details=f"Automatic backups {'enabled' if enabled else 'disabled'}, every {interval_hours}h",
    )

    return jsonify({"message": "Backup settings updated", "enabled": enabled, "interval_hours": interval_hours})


@staff_bp.route("/backup-settings/run-now", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def run_backup_now_route():
    """Manual, out-of-band backup -- runs unconditionally regardless of
    the enabled toggle/interval (see services/backup_service.py's
    run_backup_now docstring), same as clicking "Backup Now" ought to
    mean "do it right now," not "do it if the schedule agrees.\""""
    ok = run_backup_now()
    if not ok:
        return (
            jsonify({"error": "Backup failed -- check the server log for details."}),
            500,
        )

    log_action(
        staff_id=g.staff["id"],
        action="ran_manual_backup",
        target_table="app_settings",
        details="Triggered an on-demand database backup from the System page",
    )
    return jsonify({"message": "Backup completed", "backups": _list_backups()})


@staff_bp.route("/backup-settings/download/<filename>")
@roles_required("admin")
def download_backup(filename):
    if not _BACKUP_FILENAME_RE.match(filename):
        return jsonify({"error": "Invalid backup filename"}), 400

    path = os.path.realpath(os.path.join(backup_db.DEFAULT_BACKUP_DIR, filename))
    backup_dir_real = os.path.realpath(backup_db.DEFAULT_BACKUP_DIR)
    # Belt-and-suspenders on top of the regex: confirms the resolved path actually lands inside the backup dir before handing it to send_file.
    if not path.startswith(backup_dir_real + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "Backup not found"}), 404

    return send_file(path, as_attachment=True, download_name=filename)


@staff_bp.route("/backup-settings/delete/<filename>", methods=["DELETE"])
@roles_required("admin")
@limiter.limit("20 per minute")
def delete_backup(filename):
    """Manually removes one backup file -- same filename allowlist +
    realpath-containment check as download_backup above, so this can only
    ever touch a real backup file this app itself created, never an
    arbitrary path. Independent of the automatic KEEP_LAST_N pruning
    backup_database() already does on every new backup (backup_db.py) --
    this is just the manual "I don't want this one anymore" door,
    triggered from the file list itself (staff/_backup_file_list.html)."""
    if not _BACKUP_FILENAME_RE.match(filename):
        return jsonify({"error": "Invalid backup filename"}), 400

    path = os.path.realpath(os.path.join(backup_db.DEFAULT_BACKUP_DIR, filename))
    backup_dir_real = os.path.realpath(backup_db.DEFAULT_BACKUP_DIR)
    if not path.startswith(backup_dir_real + os.sep) or not os.path.isfile(path):
        return jsonify({"error": "Backup not found"}), 404

    os.remove(path)

    log_action(
        staff_id=g.staff["id"],
        action="deleted_backup",
        target_table="app_settings",
        details=f"Deleted database backup file: {filename}",
    )

    # Re-rendered list swaps straight back into #backup-file-list -- no full page reload
    # needed just to drop one row, unlike Backup Now/Save Settings which also refresh
    # the "Last backup" timestamp outside this partial.
    return render_template("staff/_backup_file_list.html", backups=_list_backups())


# # # ARCHIVE AUTO-PURGE --------------------------------------------------------
# Admin-only System page card for how long an archived/deactivated asset, member, or staff
# record sits before archive_purge_service's scheduled job permanently deletes it (or "None"
# to turn it off). One shared retention window across all three; same shape as Database Backups above.

# Preset retention windows in days shown on the System page -- 0 is "None" (off); an admin
# can still type any other custom number of days, these are just quick-pick shortcuts.
ARCHIVE_PURGE_PRESETS_DAYS = [0, 7, 30, 180, 365]


def _archived_record_counts(conn):
    """{assets, members, staff} counts of what's currently sitting
    archived/deactivated -- shown on the manage-form modal so an admin
    can see what a retention window would actually apply to before
    setting one."""
    assets = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NOT NULL"
    ).fetchone()["cnt"]
    members = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 0"
    ).fetchone()["cnt"]
    staff = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE is_active = 0"
    ).fetchone()["cnt"]
    return {"assets": assets, "members": members, "staff": staff}


@staff_bp.route("/archive-purge-settings/manage-form")
@roles_required("admin")
def manage_archive_purge_settings_form():
    conn = get_db_connection()
    purge_days = get_archive_purge_days(conn)
    counts = _archived_record_counts(conn)
    conn.close()
    return render_template(
        "staff/_archive_purge_settings_modal.html",
        purge_days=purge_days,
        counts=counts,
        presets=ARCHIVE_PURGE_PRESETS_DAYS,
    )


@staff_bp.route("/archive-purge-settings/update", methods=["POST"])
@roles_required("admin")
@limiter.limit("20 per minute")
def update_archive_purge_settings():
    data, error = get_json_or_error()
    if error:
        return error

    try:
        purge_days = int(data.get("purge_days"))
    except (TypeError, ValueError):
        return jsonify({"error": "purge_days must be a whole number"}), 400
    # 0 = "None" (disabled); 3650 (10 years) is a sanity ceiling on the custom-day input,
    # same spirit as the backup-interval cap -- keeps a fat-fingered value from silently meaning "never".
    if purge_days < 0 or purge_days > 3650:
        return jsonify({"error": "purge_days must be between 0 (None) and 3650"}), 400

    conn = get_db_connection()
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'archive_purge_days'",
        (str(purge_days),),
    )
    conn.commit()
    conn.close()

    log_action(
        staff_id=g.staff["id"],
        action="updated_archive_purge_settings",
        target_table="app_settings",
        details=(
            f"Auto-purge of archived assets/deactivated members & staff set to {purge_days} day(s)"
            if purge_days > 0
            else "Auto-purge of archived assets/deactivated members & staff turned off (None)"
        ),
    )

    return jsonify({"message": "Auto-purge settings updated", "purge_days": purge_days})


@staff_bp.route("/archive-purge-settings/purge-now", methods=["POST"])
@roles_required("admin")
@limiter.limit("10 per minute")
def run_archive_purge_now():
    """Manual, out-of-band purge -- runs the exact same three passes as
    the scheduled job (services/archive_purge_service.py: assets, then
    staff, then members), right now, but only if a retention window is
    actually set. Unlike "Backup Now" (which always runs regardless of
    the enabled toggle), this respects the current purge_days setting
    rather than offering a way to permanently delete everything
    archived/deactivated on a whim -- an admin who wants that should set
    a window first."""
    conn = get_db_connection()
    purge_days = get_archive_purge_days(conn)
    conn.close()
    if purge_days <= 0:
        return (
            jsonify({"error": "Set a retention window before purging -- it's currently set to None."}),
            400,
        )

    purged_assets = purge_expired_archived_assets()
    purged_staff = purge_expired_inactive_staff()
    purged_members = purge_expired_inactive_members()

    log_action(
        staff_id=g.staff["id"],
        action="ran_manual_archive_purge",
        target_table="assets",
        details=(
            "Triggered an on-demand archive purge from the System page -- "
            f"{purged_assets} asset(s), {purged_members} member(s), {purged_staff} staff account(s) permanently deleted"
        ),
    )
    return jsonify(
        {
            "message": f"Purged {purged_assets} asset(s), {purged_members} member(s), {purged_staff} staff account(s)",
            "purged_assets": purged_assets,
            "purged_members": purged_members,
            "purged_staff": purged_staff,
        }
    )
