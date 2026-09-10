"""Query-building ug mutation logic para sa routes/staff.py (staff accounts, audit
log, admin-deactivation approvals, loan limits, CSV import/export, system settings,
backups, archive auto-purge, online-staff widget, ug barcode-label audit logging).

Same convention sa services/member_service.py ug services/asset_service.py: every
function mag-take og already-open `conn` ug mag-return og `(data, error)`, diin ang
`error` kay `None` kung success or `(error_payload, status, context)` kung fail. Pila
ka simple "update one setting" endpoints kay mag-return na lang og `(data, error_msg)`
nga plain string message, kay ang mga routes ana kay mag-re-render ra sa ilang own
modal or mag-return og JSON 400 - wala nay extra context object nga kailangan didto.
"""

import csv
import io
import logging
import os
import re
import threading
from datetime import datetime, timedelta

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
from werkzeug.security import generate_password_hash, check_password_hash

import backup_db
from database import get_db_connection, sqlite3
from services.admin_deactivation_service import (
    determine_approval_tier,
    finalize_admin_deactivation,
    is_eligible_reviewer,
    get_review_counts,
)
from services.archive_purge_service import (
    purge_expired_archived_assets,
    purge_expired_inactive_staff,
    purge_expired_inactive_members,
)
from services.backup_service import get_last_backup_at, run_backup_now
from services.notification_service import create_admin_deactivation_requested_notifications
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
    get_alarm_enabled,
    get_staff_code_prefix,
    BARCODE_PREFIX_PATTERN,
)
from utils.formatters import full_name
from utils.logger import log_action
from utils.uploads import save_staff_photo, delete_staff_photo
from utils.validators import validate_date_string, parse_pagination_params, escape_like_wildcards

logger = logging.getLogger(__name__)

VALID_ROLES = ("admin", "in_charge", "tool_keeper")
# Caps a manual lock at 1 year, so a typo'd value can't quietly become indefinite.
MAX_MANUAL_LOCK_MINUTES = 525600

# "name" maps to two columns since there's no single stored full-name field, same as members.SORTABLE_COLUMNS.
SORTABLE_STAFF_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "username": "staff.username",
    "staff_code": "staff.staff_code",
    "role": "staff.role",
    "department": "departments.name",
}

# Days before expiry USAGE flips to "Expiring Soon"; fixed since assignment windows run in months.
USAGE_EXPIRING_SOON_DAYS = 7

# Accepts "YYYY-MM-DD" with or without a time part; returns None on bad values, never raises.
_ASSIGNED_END_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")

REQUIRED_STAFF_CSV_COLUMNS = ("member_identifier", "username", "password", "role")
OPTIONAL_STAFF_CSV_COLUMNS = ("assigned_start", "assigned_end")

# Excludes password_hash, locked_until, failed_login_attempts - security-internal, no business in a spreadsheet.
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

_NOTIFY_PREF_COLUMNS = ("notify_due_soon", "notify_overdue", "notify_requests")

# Preset retention windows in days; 0 is "None" (off); a custom value is still allowed.
ARCHIVE_PURGE_PRESETS_DAYS = [0, 7, 30, 180, 365]

# Matches backup_database()'s filename format, the allowlist that keeps downloads to real backup files.
_BACKUP_FILENAME_RE = re.compile(r"^toolroom_\d{8}_\d{6}\.db$")


def _no_ctx(error_payload, status):
    """Shortcut para sa error returns nga walay extra render context - just wraps
    it into the `(error_payload, status, {})` shape nga gina-expect sa caller."""
    return (error_payload, status, {})


# # # USAGE STATUS ---------------------------------------------------------


def _parse_assigned_end(value):
    """Mag-parse sa assigned_end string ngadto sa date object, tries duha ka
    formats. Mo-return og None kung bad/empty value, dili mo-raise."""
    for fmt in _ASSIGNED_END_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def compute_usage_status(role, assigned_end):
    """USAGE badge ug remaining-days sa staff account; admin kay permanent
    gyud, in_charge/tool_keeper ang naay real assigned window."""
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


# # # LIST / VIEW ------------------------------------------------------------


def get_staff_table_context(conn, args, limit, offset):
    """Shared query para sa staff table, gina-gamit sa duha ka htmx partial ug
    full-page route."""
    q = args.get("q", "").strip()
    role = args.get("role")
    department_id = args.get("department_id")
    show_inactive = args.get("show_inactive") == "1"
    # `status` is the fine-grained stat-card filter (active/locked/inactive), separate from the show_inactive toggle.
    status = args.get("status")
    valid_statuses = ("active", "locked", "inactive")

    from flask import jsonify

    if role and role not in VALID_ROLES:
        return None, (jsonify({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}), 400)

    if status and status not in valid_statuses:
        return None, (jsonify({"error": f"status must be one of: {', '.join(valid_statuses)}"}), 400)

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
        # show_inactive filters to *only* inactive rows; "show all" used to hide reactivations.
        where = ["staff.is_active = 0"] if show_inactive else ["staff.is_active = 1"]
    params = []
    if q:
        where.append(
            "(staff.username LIKE ? ESCAPE '\\' OR staff.staff_code LIKE ? ESCAPE '\\'"
            " OR members.first_name LIKE ? ESCAPE '\\' OR members.last_name LIKE ? ESCAPE '\\')"
        )
        like = f"%{escape_like_wildcards(q)}%"
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
            jsonify({"error": f"sort_by must be one of: {', '.join(SORTABLE_STAFF_COLUMNS)}"}),
            400,
        )
    sort_dir = args.get("sort_dir", "asc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, (jsonify({"error": "sort_dir must be 'asc' or 'desc'"}), 400)

    dir_sql = sort_dir.upper()
    order_by_sql = ", ".join(
        f"{col.strip()} {dir_sql}" for col in SORTABLE_STAFF_COLUMNS[sort_by].split(",")
    )

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

    # Converts to plain dicts to bolt on usage_status, which is derived, not a real column.
    staff_list = [dict(row) for row in staff_list]
    for row in staff_list:
        row["usage_status"], row["usage_days_left"] = compute_usage_status(row["role"], row["assigned_end"])

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


def get_staff_stat_cards(conn):
    """Nag-back sa stat cards sa staff_page() didto sa routes/pages.py."""
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM staff").fetchone()["cnt"]
    # "Locked" only counts an unexpired lockout; once locked_until passes it
    # counts as Active instead (should still sum to stat_all).
    stat_locked = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM staff
        WHERE is_active = 1
          AND locked_until IS NOT NULL
          AND locked_until > datetime('now', 'localtime')
        """
    ).fetchone()["cnt"]
    stat_active = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM staff
        WHERE is_active = 1
          AND (locked_until IS NULL OR locked_until <= datetime('now', 'localtime'))
        """
    ).fetchone()["cnt"]
    stat_inactive = conn.execute("SELECT COUNT(*) AS cnt FROM staff WHERE is_active = 0").fetchone()["cnt"]
    return {
        "stat_all": stat_all,
        "stat_locked": stat_locked,
        "stat_active": stat_active,
        "stat_inactive": stat_inactive,
    }


def get_staff_by_id(conn, staff_id):
    """Basic staff+member+department lookup by id, plain dict, walay derived fields."""
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
        (staff_id,),
    ).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    return dict(staff), None


def find_staff_id_by_barcode(conn, barcode):
    """Exact-match barcode lookup para sa search box's Enter key, through sa
    naka-link nga member's barcode."""
    row = conn.execute(
        """
        SELECT staff.id
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE members.barcode_id = ?
        """,
        (barcode,),
    ).fetchone()
    return row["id"] if row else None


def get_staff_profile(conn, staff_id):
    """Read-only profile modal; Activity tab kay admin-only ra kay ang
    audit-log mag-filter ra by staff_id para sa admins."""
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
        (staff_id,),
    ).fetchone()

    # Same idea as member_profile's pending_unrestriction_request - surfaces a pending deactivation request on the profile itself.
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
            (staff_id,),
        ).fetchone()

    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    usage_status, usage_days_left = compute_usage_status(staff_member["role"], staff_member["assigned_end"])

    return {
        "staff_member": staff_member,
        "pending_deactivation_request": pending_deactivation_request,
        "usage_status": usage_status,
        "usage_days_left": usage_days_left,
    }, None


def get_staff_for_full_activity(conn, staff_id):
    """Minimal lookup (id + name lang) para sa "View full activity" link,
    gina-verify ra nga naa gyud ang staff before mag-render sa audit log page."""
    staff_member = conn.execute(
        """
        SELECT staff.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()
    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    return staff_member, None


def get_audit_log_context(conn, args, staff):
    """Shared query para sa audit trail; admin makahimo mag-filter by kang
    kinsa nga staff_id, ang uban kay forced ra sa ilang kaugalingon."""
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
        # `until` is a bare date but created_at is a timestamp; compare against next day's start.
        until_exclusive = (datetime.strptime(until, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        filters.append("audit_log.created_at < ?")
        params.append(until_exclusive)

    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM audit_log {where_clause}", params
    ).fetchone()["cnt"]

    logs = conn.execute(
        f"""
        SELECT audit_log.*, staff.username AS staff_username,
               members.first_name AS staff_first_name, members.last_name AS staff_last_name,
               members.middle_name AS staff_middle_name, members.suffix AS staff_suffix
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        LEFT JOIN members ON staff.member_id = members.id
        {where_clause}
        ORDER BY audit_log.created_at DESC
        LIMIT ? OFFSET ?
        """,
        (*params, limit, offset),
    ).fetchall()

    return {"logs": logs, "total_count": total_count, "limit": limit, "offset": offset}, None


# # # CREATE (admin only) ------------------------------------------------


def search_eligible_members(conn, q, browse_all, offset, page_size):
    """Add Staff's member picker; nag-match sa name/code/barcode para sa
    active non-staff members ra, capped ang results."""
    if browse_all:
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
            (page_size + 1, offset),
        ).fetchall()

        has_more = len(rows) > page_size
        results = rows[:page_size]
        loaded_so_far = offset + len(results)
        next_offset = offset + page_size
        return {
            "results": results,
            "has_more": has_more,
            "loaded_so_far": loaded_so_far,
            "next_offset": next_offset,
        }

    like = f"%{escape_like_wildcards(q)}%"
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
            members.first_name LIKE ? ESCAPE '\\' OR members.last_name LIKE ? ESCAPE '\\'
            OR members.member_code LIKE ? ESCAPE '\\' OR members.barcode_id LIKE ? ESCAPE '\\'
          )
        ORDER BY members.last_name, members.first_name
        LIMIT 8
        """,
        (like, like, like, like),
    ).fetchall()

    return {"results": results, "has_more": False, "loaded_so_far": len(results), "next_offset": 0}


def create_staff(conn, data, staff_id):
    """Mag-insert og bag-ong staff account gikan sa existing member; ang
    staff_code kay auto-generate as `{prefix}{id:05d}` human sa insert, kay
    kinahanglan una ang lastrowid before ma-build ang code."""
    if data["role"] not in VALID_ROLES:
        return None, _no_ctx({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}, 400)

    # Username never changes after creation ug gina-gamit isip filename
    # component sa profile photo uploads (utils/uploads.py's
    # save_staff_photo) - safe charset ra i-allow diri para dili
    # kinahanglan pa mag-sanitize downstream, ug consistent sa naa nay
    # client-side pattern sa create-staff form.
    username = (data.get("username") or "").strip()
    if not (3 <= len(username) <= 30) or not re.match(r"^[A-Za-z0-9_.-]+$", username):
        return None, _no_ctx(
            {"error": "username must be 3-30 characters: letters, numbers, or . _ - (no spaces)"}, 400
        )

    member = conn.execute("SELECT id FROM members WHERE id = ?", (data["member_id"],)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "member_id does not match an existing member"}, 404)

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
                username,
                hashed_password,
                data["role"],
                data.get("assigned_start"),
                data.get("assigned_end"),
                data.get("is_active", 1),
                staff_id,
            ),
        )
        new_staff_id = cursor.lastrowid
        generated_code = f"{get_staff_code_prefix(conn)}{new_staff_id:05d}"
        conn.execute("UPDATE staff SET staff_code = ? WHERE id = ?", (generated_code, new_staff_id))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."}, 400
        )

    log_action(
        conn=conn,
        staff_id=staff_id,
        action="created_staff",
        target_table="staff",
        target_id=new_staff_id,
        target_name=data["username"],
        details=f"Created staff account: {data['username']}",
    )

    return {"id": new_staff_id, "staff_code": generated_code}, None


# # # UPDATE ------------------------------------------------------------


def get_staff_for_edit(conn, staff_id):
    """Field set para sa Edit Staff modal - naa nay rfid_id/locked_until
    unlike get_staff_by_id kay gina-display sa form."""
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
        (staff_id,),
    ).fetchone()
    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    return staff_member, None


def get_staff_for_reassign(conn, staff_id):
    """Field set para sa Reassign modal (change role/assignment window ra);
    mo-reject dayon kung admin ang staff kay wala man ni'y assignment window."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role, staff.staff_code, staff.assigned_start, staff.assigned_end,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()
    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if staff_member["role"] == "admin":
        return None, _no_ctx({"error": "Admin accounts don't have an assignment window to reassign"}, 400)
    return staff_member, None


def update_staff(conn, staff_id, data, acting_staff_id):
    """Mag-update sa role/assignment window/RFID; dili tugotan mag-demote/
    change og role ang admin diretso - kinahanglan mo-agi sa peer-approval
    deactivation flow (request_admin_deactivation) instead."""
    if "role" in data and data["role"] not in VALID_ROLES:
        return None, _no_ctx({"error": f"role must be one of: {', '.join(VALID_ROLES)}"}, 400)

    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    new_role = data.get("role", staff["role"])
    if staff["role"] == "admin" and new_role != "admin":
        return None, _no_ctx(
            {
                "error": "Cannot change an admin's role directly",
                "reason": "Demoting or deactivating an admin requires peer approval - use /staff/<id>/request-deactivation instead.",
            },
            403,
        )

    new_rfid_pin_hash = (
        generate_password_hash(data["rfid_pin"]) if data.get("rfid_pin") else staff["rfid_pin_hash"]
    )

    try:
        conn.execute(
            """
            UPDATE staff
            SET role = ?, assigned_start = ?, assigned_end = ?, rfid_id = ?, rfid_pin_hash = ?
            WHERE id = ?
            """,
            (
                new_role,
                data.get("assigned_start", staff["assigned_start"]),
                data.get("assigned_end", staff["assigned_end"]),
                # Empty string means "unassign"; normalized to NULL so the UNIQUE index doesn't collide on "".
                data.get("rfid_id", staff["rfid_id"]) or None,
                new_rfid_pin_hash,
                staff["id"],
            ),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, _no_ctx(
            {
                "error": "A database constraint was violated. Check that the RFID card isn't already assigned to another staff account."
            },
            400,
        )

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_staff",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Updated staff account: {staff['username']}",
    )

    return {"message": "Staff updated successfully"}, None


# # # PASSWORD MANAGEMENT ------------------------------------------------


def change_own_password(conn, staff, new_password, current_token):
    """Self-service password change; ang current-password/strength checks
    nahitabo na sa route, dinhi na lang ang hash + write + session-kill."""
    new_hash = generate_password_hash(new_password)
    try:
        conn.execute("UPDATE staff SET password_hash = ? WHERE id = ?", (new_hash, staff["id"]))
        # Kills every other session for this account, keeping this one so they're not logged out.
        conn.execute(
            "DELETE FROM sessions WHERE staff_id = ? AND token != ?", (staff["id"], current_token)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="changed_own_password",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} changed their own password",
    )
    return {"message": "Password changed successfully"}


# # # BARCODE + PIN LOGIN (self-service, any staff role) --------------------
#
# Reuses staff.rfid_pin_hash unchanged - see 0051_add_staff_barcode_pin_enabled.sql's
# own comment for why this deliberately isn't a separate PIN column. The
# actual login-time lookup lives in services/login_service.py
# (find_staff_for_barcode_pin_lookup/find_staff_for_barcode_pin_login);
# these three are just the Account Settings-side enable/disable/change flow.
#
# A fourth function (change_own_rfid_pin) used to live here for the
# admin-only RFID+PIN login flow's own PIN-change - deleted along with that
# whole login route (routes/auth.py:login_rfid), which is why comments below
# still occasionally compare against "the same write" - these three are now
# the only place that write happens.


def enable_barcode_pin_login(conn, staff, new_pin, current_token):
    """First-time enable: mag-set sa PIN ug mag-flip og barcode_pin_enabled
    on sa usa ka action (same session-kill, same hashing as
    change_own_password). PIN nga bag-o lang gi-type sa staff, dili naay
    existing on file - lahi sa change_own_barcode_pin's caller nga naay
    dunay overwrite, pero same ra ang DB write bisan asa."""
    new_hash = generate_password_hash(new_pin)
    try:
        conn.execute(
            "UPDATE staff SET rfid_pin_hash = ?, barcode_pin_enabled = 1 WHERE id = ?",
            (new_hash, staff["id"]),
        )
        conn.execute(
            "DELETE FROM sessions WHERE staff_id = ? AND token != ?", (staff["id"], current_token)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="enabled_barcode_pin_login",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} enabled barcode + PIN login",
    )
    return {"message": "Barcode + PIN login enabled"}


def disable_barcode_pin_login(conn, staff):
    """Mag-flip og barcode_pin_enabled off, wala gina-touch ang rfid_pin_hash
    - ang PIN mismo kay gina-leave in place (dili gina-clear) para kung
    mag-re-enable later dili na kinahanglan i-set balik gikan sa zero. Walay
    session-kill diri (lahi sa enable_barcode_pin_login): ang pag-disable
    ani wala ni-invalidate sa bisan unsa nga magamit pa gihapon ang PIN, kay
    ang login_barcode_pin() mag-check man og barcode_pin_enabled every
    attempt, dili lang usa ka beses sa PIN-set time."""
    conn.execute("UPDATE staff SET barcode_pin_enabled = 0 WHERE id = ?", (staff["id"],))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="disabled_barcode_pin_login",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} disabled barcode + PIN login",
    )
    return {"message": "Barcode + PIN login disabled"}


def change_own_barcode_pin(conn, staff, new_pin, current_token):
    """Self-service PIN change kung naka-enable na ang barcode + PIN login -
    same session-kill/hashing pattern as change_own_password, just against
    staff.rfid_pin_hash (the shared quick-login PIN column) instead."""
    new_hash = generate_password_hash(new_pin)
    try:
        conn.execute("UPDATE staff SET rfid_pin_hash = ? WHERE id = ?", (new_hash, staff["id"]))
        conn.execute(
            "DELETE FROM sessions WHERE staff_id = ? AND token != ?", (staff["id"], current_token)
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="changed_own_barcode_pin",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} changed their own barcode + PIN login PIN",
    )
    return {"message": "PIN changed successfully"}


# # # NOTIFICATION PREFERENCES --------------------------------------------


def parse_notification_pref_updates(data):
    """Nag-validate sa notify_* toggles ug toast duration.

    Returns: (updates_dict, error_msg)."""
    updates = {}
    for col in _NOTIFY_PREF_COLUMNS:
        if col in data:
            if not isinstance(data[col], bool):
                return None, f"{col} must be true or false"
            updates[col] = 1 if data[col] else 0

    if "toast_duration_seconds" in data:
        try:
            seconds = int(data["toast_duration_seconds"])
        except (TypeError, ValueError):
            return None, "toast_duration_seconds must be a whole number"
        if not (2 <= seconds <= 15):
            return None, "toast_duration_seconds must be between 2 and 15"
        updates["toast_duration_seconds"] = seconds

    if not updates:
        return None, "No recognized preference fields provided"

    return updates, None


def save_own_notification_prefs(conn, updates, staff):
    """Mag-write sa already-validated notify_*/toast_duration updates dict
    ngadto sa staff row, dayon log_action."""
    try:
        set_sql = ", ".join(f"{col} = ?" for col in updates)
        conn.execute(f"UPDATE staff SET {set_sql} WHERE id = ?", (*updates.values(), staff["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="updated_own_notification_prefs",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details="Updated own notification preferences: "
        + ", ".join(
            f"{col}={val}" if col == "toast_duration_seconds" else f"{col}={'on' if val else 'off'}"
            for col, val in updates.items()
        ),
    )
    return {"message": "Notification preferences updated", **updates}


# # # PROFILE PHOTO -----------------------------------------------------


def upload_own_photo(conn, staff, saved_path):
    """DB update para sa photo nga already validated+saved sa route; mag-delete
    lang sa old file human sa commit, para dili mawala kung mag-fail ang DB write."""
    old_photo_path = staff["photo_path"]
    try:
        conn.execute("UPDATE staff SET photo_path = ? WHERE id = ?", (saved_path, staff["id"]))
        conn.commit()
    except Exception:
        conn.rollback()
        # DB update failed; clean up the file just saved before it becomes an orphan.
        delete_staff_photo(saved_path)
        raise

    delete_staff_photo(old_photo_path)

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="updated_own_photo",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} updated their profile photo",
    )
    return {"message": "Profile photo updated successfully", "photo_path": saved_path}


def remove_own_photo(conn, staff):
    """Mag-clear sa photo_path ug mag-delete sa file on disk; no-op ra kung
    wala na'y photo, dili error."""
    old_photo_path = staff["photo_path"]
    if not old_photo_path:
        return {"message": "No profile photo to remove", "photo_url": None}

    try:
        conn.execute("UPDATE staff SET photo_path = NULL WHERE id = ?", (staff["id"],))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    delete_staff_photo(old_photo_path)

    log_action(
        conn=conn,
        staff_id=staff["id"],
        action="removed_own_photo",
        target_table="staff",
        target_id=staff["id"],
        target_name=staff["username"],
        details=f"Staff {staff['username']} removed their profile photo",
    )
    return {"message": "Profile photo removed successfully", "photo_url": None}


# # # ADMIN-INITIATED PASSWORD RESET ---------------------------------------


def get_staff_for_reset_password(conn, staff_id):
    """Field set para sa Reset Password confirm modal, plus is_active check
    since dili ni allowed sa deactivated account."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.username, staff.is_active,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()
    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    return staff_member, None


def reset_staff_password(conn, staff_id, acting_staff_id):
    """Admin-initiated reset; mag-generate og random password, ipakita usa
    lang ka beses, ug mag-kill sa existing sessions."""
    import secrets

    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if not staff["is_active"]:
        return None, _no_ctx({"error": "This account is deactivated"}, 400)

    temporary_password = secrets.token_urlsafe(12)
    new_hash = generate_password_hash(temporary_password)

    try:
        conn.execute(
            """
            UPDATE staff
            SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL
            WHERE id = ?
            """,
            (new_hash, staff_id),
        )
        conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="reset_staff_password",
        target_table="staff",
        target_id=staff_id,
        target_name=staff["username"],
        details=f"Admin reset password for staff account: {staff['username']} (temporary password issued)",
    )

    return {"message": "Password reset successfully", "temporary_password": temporary_password}, None


# # # LOCK / UNLOCK ---------------------------------------------------------


def get_staff_for_lock(conn, staff_id, acting_staff_id):
    """Field set para sa Lock modal - guards vs self-lock ug vs already-deactivated account."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.is_active, staff.locked_until,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()

    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if staff_id == acting_staff_id:
        return None, _no_ctx({"error": "You can't lock your own account"}, 403)
    if not staff_member["is_active"]:
        return None, _no_ctx({"error": "This account is already deactivated"}, 400)
    return staff_member, None


def lock_staff(conn, staff_id, acting_staff_id, indefinite, duration_minutes):
    """Manual lock, indefinite or timed; mag-kill dayon sa live sessions
    aron dili lang mo-apply sa future logins."""
    if staff_id == acting_staff_id:
        return None, _no_ctx({"error": "You can't lock your own account"}, 403)

    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if not staff["is_active"]:
        return None, _no_ctx({"error": "This account is already deactivated"}, 400)

    locked_until = (
        INDEFINITE_LOCK_UNTIL
        if indefinite
        else (datetime.now() + timedelta(minutes=duration_minutes)).strftime("%Y-%m-%d %H:%M:%S")
    )

    conn.execute("UPDATE staff SET locked_until = ? WHERE id = ?", (locked_until, staff_id))
    # A lock cuts access off now, not just future logins; live sessions get dropped too.
    conn.execute("DELETE FROM sessions WHERE staff_id = ?", (staff_id,))
    conn.commit()

    detail = (
        f"Manually locked staff account (indefinite): {staff['username']}"
        if indefinite
        else f"Manually locked staff account for {duration_minutes} minute(s): {staff['username']}"
    )
    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="locked_staff",
        target_table="staff",
        target_id=staff_id,
        target_name=staff["username"],
        details=detail,
    )

    return {"message": "Staff account locked successfully", "locked_until": locked_until}, None


def get_staff_for_unlock(conn, staff_id):
    """Field set para sa Unlock modal; mo-reject kung wala man diay na-lock ang account."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.locked_until, staff.failed_login_attempts,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()

    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if not staff_member["locked_until"] and staff_member["failed_login_attempts"] == 0:
        return None, _no_ctx({"error": "This account is not currently locked"}, 400)
    return staff_member, None


def unlock_staff(conn, staff_id, acting_staff_id):
    """Manually mag-clear sa lockout early; walay self-service recovery
    diri kay offline deployment man ni."""
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if not staff["locked_until"] and staff["failed_login_attempts"] == 0:
        return None, _no_ctx({"error": "This account is not currently locked"}, 400)

    conn.execute("UPDATE staff SET failed_login_attempts = 0, locked_until = NULL WHERE id = ?", (staff_id,))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="unlocked_staff",
        target_table="staff",
        target_id=staff_id,
        target_name=staff["username"],
        details=f"Manually cleared lockout for staff account: {staff['username']}",
    )

    return {"message": "Staff account unlocked successfully"}, None


# # # DELETE (deactivate, admin only) ------------------------------------


def get_staff_for_delete(conn, staff_id):
    """Field set para sa Deactivate modal; mo-block dayon kung admin role kay
    kailangan man na og peer-approval flow."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()

    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if staff_member["role"] == "admin":
        return None, _no_ctx({"error": "Admin account/s cannot be deactivated"}, 403)
    return staff_member, None


def deactivate_staff(conn, staff_id, acting_staff_id):
    """Single-staff deactivate; mag-clear sa assigned_start/end kay wala nay
    active assignment window ang deactivated account."""
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if staff["role"] == "admin":
        return None, _no_ctx({"error": "Admin account/s cannot be deactivated"}, 403)

    if not staff["is_active"]:
        return None, _no_ctx({"error": "Staff account is already deactivated"}, 400)

    try:
        conn.execute(
            "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
            (staff_id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."}, 400
        )

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="deactivated_staff",
        target_table="staff",
        target_id=staff_id,
        target_name=staff["username"],
        details=f"Deactivated staff account: {staff['username']}",
    )

    return {"message": "Staff deactivated successfully"}, None


def get_staff_for_reactivate(conn, staff_id):
    """Field set para sa Reactivate modal; mo-reject kung active na diay ang account."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.is_active,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()
    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if staff_member["is_active"]:
        return None, _no_ctx({"error": "Staff account is already active"}, 400)
    return staff_member, None


def reactivate_staff(conn, staff_id, acting_staff_id, reason):
    """Mag-reactivate og bisan unsa nga staff role kay kinahanglan ang acting
    admin's own password plus logged reason (ang password check mismo
    nahitabo sa route, kay didto ra naa ang g.staff)."""
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

    if staff is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if staff["is_active"]:
        return None, _no_ctx({"error": "Staff account is already active"}, 400)

    try:
        conn.execute("UPDATE staff SET is_active = 1, deactivated_at = NULL WHERE id = ?", (staff_id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."}, 400
        )

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="reactivated_staff",
        target_table="staff",
        target_id=staff_id,
        target_name=staff["username"],
        details=f"Reactivated staff account: {staff['username']} - reason: {reason}",
    )

    return {"message": "Staff reactivated successfully"}, None


def bulk_deactivate_staff(conn, ids, acting_staff_id):
    """Bulk deactivate og daghang staff dungan; admin accounts kay gina-skip,
    kay ang paggawas ana kay through peer-approval ra man."""
    deactivated, skipped = [], []

    for staff_id in ids:
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()

        if row is None:
            skipped.append({"id": staff_id, "reason": "Staff not found"})
            continue

        if row["role"] == "admin":
            skipped.append(
                {"id": staff_id, "name": row["username"], "reason": "Admin accounts require a deactivation request instead"}
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
            skipped.append({"id": staff_id, "name": row["username"], "reason": "A database constraint was violated"})

    if deactivated:
        log_action(
            conn=conn,
            staff_id=acting_staff_id,
            action="bulk_deactivated_staff",
            target_table="staff",
            details=(
                f"Bulk deactivated {len(deactivated)} staff account(s): "
                + ", ".join(s["name"] for s in deactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"deactivated_count": len(deactivated), "deactivated": deactivated, "skipped": skipped}


def bulk_reactivate_staff(conn, ids, acting_staff_id):
    """Bulk reactivate og daghang staff dungan; walay admin guard nga
    gikinahanglan kay ang reactivate never mo-agi sa peer approval."""
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
            skipped.append({"id": staff_id, "name": row["username"], "reason": "A database constraint was violated"})

    if reactivated:
        log_action(
            conn=conn,
            staff_id=acting_staff_id,
            action="bulk_reactivated_staff",
            target_table="staff",
            details=(
                f"Bulk reactivated {len(reactivated)} staff account(s): "
                + ", ".join(s["name"] for s in reactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {"reactivated_count": len(reactivated), "reactivated": reactivated, "skipped": skipped}


# # # API ROUTES (barcode-based staff lookup for login) -----------------


def get_staff_by_barcode(conn, barcode):
    """Barcode-based staff lookup para sa login flow's staff-scan step."""
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

    if result is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    return dict(result), None


# # # ADMIN DEACTIVATION (approval-gated) ----------------------------------


def get_staff_for_deactivation_request(conn, staff_id, acting_staff_id):
    """Field set para sa Request Admin Deactivation modal, plus flag kung
    naa nay pending request para ani nga admin."""
    staff_member = conn.execute(
        """
        SELECT staff.id, staff.role, staff.is_active,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff
        JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (staff_id,),
    ).fetchone()

    if staff_member is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)
    if staff_member["role"] != "admin":
        return None, _no_ctx({"error": "This form is only for deactivating admin accounts"}, 400)
    if not staff_member["is_active"]:
        return None, _no_ctx({"error": "This account is already deactivated"}, 400)
    if staff_id == acting_staff_id:
        return None, _no_ctx({"error": "Cannot request your own deactivation"}, 400)

    existing_pending = conn.execute(
        "SELECT id FROM admin_deactivation_requests WHERE target_staff_id = ? AND status = 'pending'",
        (staff_id,),
    ).fetchone()

    return {"staff_member": staff_member, "already_pending": existing_pending is not None}, None


def request_admin_deactivation(conn, target_staff_id, acting_staff, reason):
    """Mag-request og deactivation sa laing admin instead of doing it
    unilaterally; kinahanglan pa muna og approvals before ma-finalize."""
    if target_staff_id == acting_staff["id"]:
        return None, _no_ctx({"error": "Cannot request your own deactivation"}, 400)

    target = conn.execute(
        """
        SELECT staff.*, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff JOIN members ON members.id = staff.member_id
        WHERE staff.id = ?
        """,
        (target_staff_id,),
    ).fetchone()

    if target is None:
        return None, _no_ctx({"error": "Staff not found"}, 404)

    if target["role"] != "admin":
        return None, _no_ctx(
            {"error": "This route is only for deactivating admin accounts. Use /staff/<id>/delete for other roles."},
            400,
        )

    if not target["is_active"]:
        return None, _no_ctx({"error": "Staff account is already deactivated"}, 400)

    existing_pending = conn.execute(
        "SELECT id FROM admin_deactivation_requests WHERE target_staff_id = ? AND status = 'pending'",
        (target_staff_id,),
    ).fetchone()

    if existing_pending is not None:
        return None, _no_ctx(
            {
                "error": "A pending deactivation request already exists for this admin",
                "request_id": existing_pending["id"],
            },
            409,
        )

    tier, required_approvals, required_in_charge = determine_approval_tier(conn, acting_staff["id"], target_staff_id)

    if tier is None:
        return None, _no_ctx(
            {
                "error": "No eligible reviewers currently exist to process this request",
                "reason": "Deactivating this admin requires at least 1 active in_charge account plus at least 1 more in_charge or tool_keeper account - none are currently available.",
            },
            409,
        )

    target_full_name = full_name(target["first_name"], target["last_name"], target["middle_name"], target["suffix"])
    requester_member = conn.execute(
        "SELECT first_name, last_name, middle_name, suffix FROM members WHERE id = ?",
        (acting_staff["member_id"],),
    ).fetchone()
    requester_full_name = full_name(
        requester_member["first_name"], requester_member["last_name"], requester_member["middle_name"], requester_member["suffix"]
    )

    try:
        cursor = conn.execute(
            """
            INSERT INTO admin_deactivation_requests
                (target_staff_id, requested_by_staff_id, reason, required_tier, required_approvals, required_in_charge)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (target_staff_id, acting_staff["id"], reason, tier, required_approvals, required_in_charge),
        )
        request_id = cursor.lastrowid
        # Notifies every in_charge/tool_keeper, same transaction as the insert so notifications never go missing.
        create_admin_deactivation_requested_notifications(conn, request_id, target_full_name, requester_full_name)
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, _no_ctx({"error": "A database constraint was violated."}, 400)

    log_action(
        conn=conn,
        staff_id=acting_staff["id"],
        action="requested_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=request_id,
        target_name=target["username"],
        details=(
            f"Requested deactivation of admin {target['username']}: {reason} "
            f"(requires {required_approvals} approval(s), {required_in_charge}+ in_charge)"
        ),
    )

    return {
        "message": "Deactivation request submitted",
        "id": request_id,
        "required_tier": tier,
        "required_approvals": required_approvals,
        "required_in_charge": required_in_charge,
    }, None


def approve_admin_deactivation(conn, request_id, acting_staff, reason):
    """Mag-record og usa ka approve vote; dili pa mo-deactivate bisan
    naabot na ang threshold - finalize_admin_deactivation_request pa gyud
    ang mo-pull sa trigger."""
    req = conn.execute("SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)).fetchone()

    if req is None:
        return None, _no_ctx({"error": "Request not found"}, 404)

    if req["status"] != "pending":
        return None, _no_ctx({"error": f"This request has already been {req['status']}"}, 409)

    if not is_eligible_reviewer(acting_staff["role"]):
        return None, _no_ctx({"error": "Only in_charge and tool_keeper accounts can review this request"}, 403)

    if acting_staff["id"] in (req["target_staff_id"], req["requested_by_staff_id"]):
        return None, _no_ctx({"error": "You cannot approve a request you filed or are the target of"}, 403)

    try:
        conn.execute(
            "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id, decision, reason) VALUES (?, ?, 'approve', ?)",
            (request_id, acting_staff["id"], reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, _no_ctx({"error": "You have already reviewed this request"}, 400)

    # req has no join, so target_username isn't on it - look it up separately.
    target_staff_row = conn.execute("SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)).fetchone()
    log_action(
        conn=conn,
        staff_id=acting_staff["id"],
        action="approved_admin_deactivation",
        target_table="admin_deactivation_requests",
        target_id=request_id,
        target_name=target_staff_row["username"] if target_staff_row else None,
        details=f"Approved deactivation request #{request_id} for staff {req['target_staff_id']}: {reason}",
    )

    # Just records the vote; reaching the threshold alone deactivates nothing until /finalize.
    approve_count, in_charge_count, _ = get_review_counts(conn, request_id)

    return {
        "message": "Approval recorded",
        "approvals_received": approve_count,
        "approvals_required": req["required_approvals"],
        "in_charge_received": in_charge_count,
        "in_charge_required": req["required_in_charge"],
    }, None


def deny_admin_deactivation(conn, request_id, acting_staff, denial_reason):
    """Ang deny kay recorded vote ra; ang requester's own finalize or cancel
    ra gyud ang mo-resolve sa request."""
    req = conn.execute("SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)).fetchone()

    if req is None:
        return None, _no_ctx({"error": "Request not found"}, 404)

    if req["status"] != "pending":
        return None, _no_ctx({"error": f"This request has already been {req['status']}"}, 409)

    is_requester = acting_staff["id"] == req["requested_by_staff_id"]
    is_eligible_denier = is_eligible_reviewer(acting_staff["role"]) and acting_staff["id"] not in (
        req["target_staff_id"],
        req["requested_by_staff_id"],
    )

    if not (is_requester or is_eligible_denier):
        return None, _no_ctx(
            {
                "error": "You are not authorized to deny this request",
                "reason": "Only the requester (to cancel it) or an in_charge/tool_keeper account (other than the requester or target) can deny this request",
            },
            403,
        )

    # A deny vote needs a reason for the requester; a self-cancel doesn't need one.
    if not is_requester and not denial_reason:
        return None, _no_ctx({"error": "A reason is required to deny this request"}, 400)

    # Looked up before the request resolves - req has no target_username column of its own.
    target_staff_row = conn.execute("SELECT username FROM staff WHERE id = ?", (req["target_staff_id"],)).fetchone()

    if is_requester:
        conn.execute(
            """
            UPDATE admin_deactivation_requests
            SET status = 'cancelled', denied_by_staff_id = ?, denial_reason = ?, resolved_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (acting_staff["id"], denial_reason, request_id),
        )
        conn.commit()
        log_action(
            conn=conn,
            staff_id=acting_staff["id"],
            action="cancelled_admin_deactivation",
            target_name=target_staff_row["username"] if target_staff_row else None,
            target_table="admin_deactivation_requests",
            target_id=request_id,
            details=f"Request #{request_id} cancelled" + (f": {denial_reason}" if denial_reason else ""),
        )
        return {"message": "Request cancelled"}, None

    try:
        conn.execute(
            "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id, decision, reason) VALUES (?, ?, 'deny', ?)",
            (request_id, acting_staff["id"], denial_reason),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, _no_ctx({"error": "You have already reviewed this request"}, 400)

    log_action(
        conn=conn,
        staff_id=acting_staff["id"],
        action="denied_admin_deactivation",
        target_name=target_staff_row["username"] if target_staff_row else None,
        target_table="admin_deactivation_requests",
        target_id=request_id,
        details=f"Denied deactivation request #{request_id}" + (f": {denial_reason}" if denial_reason else ""),
    )

    _approve_count, _in_charge_count, deny_count = get_review_counts(conn, request_id)
    return {"message": "Deny recorded", "denies_received": deny_count}, None


def finalize_admin_deactivation_request(conn, request_id, acting_staff_id):
    """Ang requester's own final say; mag-deactivate sa target, pero only
    kung sila mismo ang mo-pull sa trigger."""
    req = conn.execute("SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)).fetchone()

    if req is None:
        return None, _no_ctx({"error": "Request not found"}, 404)

    if req["status"] != "pending":
        return None, _no_ctx({"error": f"This request has already been {req['status']}"}, 409)

    if acting_staff_id != req["requested_by_staff_id"]:
        return None, _no_ctx({"error": "Only the staff member who filed this request can finalize it"}, 403)

    ok, error_message = finalize_admin_deactivation(req, acting_staff_id, conn)

    if not ok:
        return None, _no_ctx({"error": error_message}, 409)

    return {"message": "Admin account deactivated"}, None


def get_admin_deactivation_reviews(conn, request_id):
    """Every vote sa usa ka request plus kinsa ang nag-cast ug ngano,
    admin-only ni; ang uban makakita ra sa aggregate counts."""
    req = conn.execute("SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)).fetchone()

    if req is None:
        return None, _no_ctx({"error": "Request not found"}, 404)

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
        (request_id,),
    ).fetchall()

    target_staff_row = conn.execute(
        "SELECT username, member_id FROM staff WHERE id = ?", (req["target_staff_id"],)
    ).fetchone()
    target_member = (
        conn.execute("SELECT * FROM members WHERE id = ?", (target_staff_row["member_id"],)).fetchone()
        if target_staff_row
        else None
    )
    approve_count, in_charge_count, deny_count = get_review_counts(conn, request_id)

    return {
        "req": req,
        "reviews": reviews,
        "target_member": target_member,
        "approve_count": approve_count,
        "in_charge_count": in_charge_count,
        "deny_count": deny_count,
        "requirements_met": (approve_count >= req["required_approvals"] and in_charge_count >= req["required_in_charge"]),
    }, None


def list_admin_deactivation_requests(conn, args, acting_staff_id, limit, offset):
    """Paginated list sa requests, naay optional status filter ug `mine`
    toggle para sa "requests I filed" view; tag-iya nga vote (`my_decision`)
    gina-include para sa acting staff."""
    valid_statuses = ("pending", "approved", "denied", "cancelled")
    status_filter = args.get("status")
    if status_filter and status_filter not in valid_statuses:
        return None, _no_ctx({"error": f"status must be one of: {', '.join(valid_statuses)}"}, 400)

    where_clauses = []
    params = []

    if args.get("mine") == "1":
        # Same opt-in narrowing as list_unrestriction_requests's `mine` param.
        where_clauses.append("admin_deactivation_requests.requested_by_staff_id = ?")
        params.append(acting_staff_id)

    if status_filter:
        where_clauses.append("admin_deactivation_requests.status = ?")
        params.append(status_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM admin_deactivation_requests {where_sql}", params
    ).fetchone()["cnt"]

    # my_decision's staff id param goes first; it's bound in SELECT, which precedes WHERE.
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
        [acting_staff_id] + params + [limit, offset],
    ).fetchall()

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "status_filter": status_filter or "",
        "requests_list": rows,
    }, None


# # # LOAN LIMITS (per-member-type concurrent-loan caps) ----------------------


def get_loan_limits(conn):
    """Tanang per-member-type concurrent-loan caps (trainee/trainer), ordered by member_type."""
    return conn.execute("SELECT * FROM loan_limits ORDER BY member_type").fetchall()


def parse_loan_limit_updates(data):
    """Nag-validate sa trainee/trainer loan-limit inputs (whole number, at least 1).

    Returns: (updates_dict, error_msg)."""
    updates = {}
    for member_type in ("trainee", "trainer"):
        raw = data.get(f"max_concurrent_loans_{member_type}")
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None, f"Loan limit for {member_type} must be a whole number"
        if value < 1:
            return None, f"Loan limit for {member_type} must be at least 1"
        updates[member_type] = value
    return updates, None


def save_loan_limits(conn, updates, acting_staff_id):
    """Mag-write sa validated loan-limit updates dict ngadto sa loan_limits table."""
    for member_type, value in updates.items():
        conn.execute(
            "UPDATE loan_limits SET max_concurrent_loans = ? WHERE member_type = ?", (value, member_type)
        )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_loan_limits",
        target_table="loan_limits",
        details=f"Set concurrent-loan limits: trainee={updates['trainee']}, trainer={updates['trainer']}",
    )
    return {"message": "Loan limits updated successfully", **updates}


# # # BULK IMPORT (CSV) ------------------------------------------------------


def import_staff_csv(content, mode, acting_staff_id):
    """Bulk-create og staff gikan sa CSV; kada row kay mag-reference sa
    existing eligible member, dili bag-ong member. `mode="preview"` mag-run
    ra sa dry_run (walay commit) para makita una ang errors before i-import gyud."""
    dry_run = mode == "preview"

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []
    header_map = {h: (h or "").strip().lower().replace(" ", "_") for h in raw_fieldnames}
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_STAFF_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return None, _no_ctx({"error": f"CSV missing required column(s): {', '.join(missing_cols)}"}, 400)

    conn = get_db_connection()
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors = [], []

    try:
        _import_staff_rows(reader, header_map, conn, acting_staff_id, created, errors)
        if dry_run:
            conn.execute("ROLLBACK")
        else:
            conn.execute("COMMIT")
    except Exception:
        # Anything past a per-row IntegrityError (already caught below) is unexpected --
        # roll back the still-open transaction so a bug here can't leave the DB write-locked.
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if dry_run:
        return {"dry_run": True, "rows_to_create": len(created), "errors": errors}, None

    log_action(
        staff_id=acting_staff_id,
        action="bulk_imported_staff",
        target_table="staff",
        details=f"Imported {len(created)} staff account(s) via CSV, {len(errors)} error(s)",
    )

    return {"dry_run": False, "created_count": len(created), "created": created, "errors": errors}, None


def _import_staff_rows(reader, header_map, conn, acting_staff_id, created, errors):
    """Ang per-row body sa import_staff_csv(), gi-separate para ang outer
    function's try/finally makaguarantee nga mo-run gyud ang conn.close()
    bisan unsay mahitabo diri. Kada row naay own SAVEPOINT para usa lang ka
    bad row ang ma-skip, dili mo-abort ang tibuok import."""
    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        member_identifier = (row.get("member_identifier") or "").strip()
        username = (row.get("username") or "").strip()
        password = row.get("password") or ""
        role = (row.get("role") or "").strip().lower()

        if not member_identifier or not username or not password or not role:
            errors.append({"row": row_num, "error": "member_identifier, username, password, and role are required"})
            continue

        if role not in VALID_ROLES:
            errors.append({"row": row_num, "error": f"role must be one of: {', '.join(VALID_ROLES)}"})
            continue

        if len(password) < 8:
            errors.append({"row": row_num, "error": "password must be at least 8 characters"})
            continue

        # Same charset rule as create_staff() - kay gina-gamit sab ni sa
        # profile photo filename later (save_staff_photo).
        if not (3 <= len(username) <= 30) or not re.match(r"^[A-Za-z0-9_.-]+$", username):
            errors.append(
                {"row": row_num, "error": "username must be 3-30 characters: letters, numbers, or . _ - (no spaces)"}
            )
            continue

        member = conn.execute(
            "SELECT id FROM members WHERE member_code = ? OR barcode_id = ?",
            (member_identifier, member_identifier),
        ).fetchone()
        if member is None:
            errors.append({"row": row_num, "error": f"No member found matching member_identifier '{member_identifier}'"})
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
                    acting_staff_id,
                ),
            )
            new_id = cursor.lastrowid
            conn.execute("UPDATE staff SET staff_code = ? WHERE id = ?", (f"{get_staff_code_prefix(conn)}{new_id:05d}", new_id))
            conn.execute("RELEASE row_import")
            created.append({"row": row_num, "id": new_id, "username": username})
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append({"row": row_num, "error": f"duplicate or constraint violation: {e}"})


# # # EXPORT --------------------------------------------------------------


def default_staff_export_basename(show_inactive):
    """Default filename (walay extension pa) para sa staff export, e.g. staff_active_20260908."""
    return f"staff_{'inactive' if show_inactive else 'active'}_{datetime.now().strftime('%Y%m%d')}"


def resolve_staff_export_columns(selected):
    """Filters STAFF_EXPORT_COLUMNS down sa gi-select nga columns, pero
    mo-fallback sa tanan kung walay valid match (empty/invalid selection)."""
    if selected:
        columns = [c for c in STAFF_EXPORT_COLUMNS if c in selected]
        if not columns:
            columns = list(STAFF_EXPORT_COLUMNS)
    else:
        columns = list(STAFF_EXPORT_COLUMNS)
    return columns


def get_staff_for_export(conn, show_inactive):
    """Raw rows para sa staff export, active or inactive lang depende sa toggle."""
    where_clause = "staff.is_active = 0" if show_inactive else "staff.is_active = 1"
    return conn.execute(
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


def build_staff_export_file(staff_rows, columns, export_format, show_inactive):
    """Mag-build sa xlsx or csv file bytes gikan sa staff rows + columns.

    Returns: (file_bytes, content_type, export_format)."""
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
            longest = max([len(col)] + [len(str(row[col] or "")) for row in staff_rows])
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 40)
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([STAFF_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
    for row in staff_rows:
        writer.writerow([row[col] if row[col] is not None else "" for col in columns])
    return output.getvalue().encode("utf-8-sig"), "text/csv", "csv"


def save_export_copy(file_bytes, save_dir, filename):
    """Best-effort server-side copy sa usa ka export; dili ni mag-fail sa
    tibuok request kung dili ma-save - warning ra i-return.

    Returns: (saved_path_or_None, warning_or_None)."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        saved_path = os.path.join(save_dir, filename)
        with open(saved_path, "wb") as f:
            f.write(file_bytes)
        return saved_path, None
    except OSError as e:
        logger.warning(f"Could not save export copy to disk: {e}")
        return None, "Could not save a copy on the server; your download still completed."


# # # SYSTEM SETTINGS (session/security/notification/loan/damage thresholds) --
# All six follow the same shape: parse int field(s), validate range, write to
# app_settings, log_action. Returns (result_dict, error_msg) - error_msg is a
# plain string the route either jsonifies (400) or re-renders its own modal with.


def update_session_settings(conn, data, acting_staff_id):
    """Mag-set sa session_idle_minutes ug session_absolute_hours app_settings."""
    try:
        idle_minutes = int(data.get("session_idle_minutes"))
    except (TypeError, ValueError):
        return None, "Idle timeout must be a whole number of minutes"
    if not (1 <= idle_minutes <= 1440):
        return None, "Idle timeout must be between 1 and 1440 minutes (24 hours)"

    try:
        absolute_hours = int(data.get("session_absolute_hours"))
    except (TypeError, ValueError):
        return None, "Absolute session length must be a whole number of hours"
    if not (1 <= absolute_hours <= 720):
        return None, "Absolute session length must be between 1 and 720 hours (30 days)"

    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'session_idle_minutes'", (str(idle_minutes),))
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'session_absolute_hours'", (str(absolute_hours),))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_session_settings",
        target_table="app_settings",
        details=f"Set session_idle_minutes={idle_minutes}, session_absolute_hours={absolute_hours}",
    )

    return {
        "message": "Session settings updated successfully",
        "session_idle_minutes": idle_minutes,
        "session_absolute_hours": absolute_hours,
    }, None


def update_account_security(conn, data, acting_staff_id):
    """Mag-set sa max_failed_attempts ug lockout_minutes app_settings."""
    try:
        max_attempts = int(data.get("max_failed_attempts"))
    except (TypeError, ValueError):
        return None, "Max failed attempts must be a whole number"
    if not (1 <= max_attempts <= 20):
        return None, "Max failed attempts must be between 1 and 20"

    try:
        lockout_minutes = int(data.get("lockout_minutes"))
    except (TypeError, ValueError):
        return None, "Lockout duration must be a whole number of minutes"
    if not (1 <= lockout_minutes <= 1440):
        return None, "Lockout duration must be between 1 and 1440 minutes (24 hours)"

    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'max_failed_attempts'", (str(max_attempts),))
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'lockout_minutes'", (str(lockout_minutes),))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_account_security_settings",
        target_table="app_settings",
        details=f"Set max_failed_attempts={max_attempts}, lockout_minutes={lockout_minutes}",
    )

    return {
        "message": "Account security settings updated successfully",
        "max_failed_attempts": max_attempts,
        "lockout_minutes": lockout_minutes,
    }, None


def update_notification_settings(conn, data, acting_staff_id):
    """Mag-set sa due_soon_hours app_setting (kanus-a mag-flag ang "due soon" sa loans)."""
    try:
        due_soon_hours = int(data.get("due_soon_hours"))
    except (TypeError, ValueError):
        return None, "Due-soon threshold must be a whole number of hours"
    if not (1 <= due_soon_hours <= 168):
        return None, "Due-soon threshold must be between 1 and 168 hours (1 week)"

    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'due_soon_hours'", (str(due_soon_hours),))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_notification_settings",
        target_table="app_settings",
        details=f"Set due_soon_hours={due_soon_hours}",
    )

    return {"message": "Notification settings updated successfully", "due_soon_hours": due_soon_hours}, None


def update_loan_duration(conn, data, acting_staff_id):
    """Mag-set sa default_loan_days app_setting."""
    try:
        default_loan_days = int(data.get("default_loan_days"))
    except (TypeError, ValueError):
        return None, "Default loan duration must be a whole number of days"
    if not (1 <= default_loan_days <= 90):
        return None, "Default loan duration must be between 1 and 90 days"

    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'default_loan_days'", (str(default_loan_days),))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_loan_duration",
        target_table="app_settings",
        details=f"Set default_loan_days={default_loan_days}",
    )

    return {"message": "Loan duration updated successfully", "default_loan_days": default_loan_days}, None


def update_checkout_session_settings(conn, data, acting_staff_id):
    """Kutob asa katagal ang usa ka in-progress checkout cart makapabilin
    untouched before mag-auto-cancel ug ma-release ang reserved assets -
    kaniadto hardcoded 20-minute constant (utils/constants.py), karon
    admin/in_charge-editable na nga app_settings row parehas sa uban
    operational knobs ani nga page."""
    try:
        stale_minutes = int(data.get("checkout_session_stale_minutes"))
    except (TypeError, ValueError):
        return None, "Checkout session timeout must be a whole number of minutes"
    if not (1 <= stale_minutes <= 1440):
        return None, "Checkout session timeout must be between 1 and 1440 minutes (24 hours)"

    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'checkout_session_stale_minutes'", (str(stale_minutes),)
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_checkout_session_settings",
        target_table="app_settings",
        details=f"Set checkout_session_stale_minutes={stale_minutes}",
    )

    return {
        "message": "Checkout session timeout updated successfully",
        "checkout_session_stale_minutes": stale_minutes,
    }, None


# # # MAINTENANCE SCHEDULING (admin/in_charge-editable defaults) --------------


def update_maintenance_settings(conn, data, acting_staff_id):
    """Duha ka independent operational defaults para sa preventive
    maintenance scheduling (migrations 0055/0059) - kaniadto seeded usa lang
    ka beses ug wala pa gina-expose sa admin/in_charge account para ma-change:
    - default_maintenance_interval_days: kutob asa ka layo ang next_maintenance_due
      sa bag-ong-gibuhat (or re-serviced) serialized asset mag-auto-compute
      kung wala mag-set ang staff og explicit value - services/asset_service.py's
      _default_next_maintenance_due.
    - maintenance_reminder_lead_days: pila ka days before due date (or kutob
      asa ka duol sa usage-interval trigger) ang mag-count as "due soon" para
      sa dashboard card ug sa scheduled notification sweep -
      services/maintenance_service.py's sweep_maintenance_due."""
    try:
        interval_days = int(data.get("default_maintenance_interval_days"))
    except (TypeError, ValueError):
        return None, "Default maintenance interval must be a whole number of days"
    if not (1 <= interval_days <= 3650):
        return None, "Default maintenance interval must be between 1 and 3650 days (10 years)"

    try:
        lead_days = int(data.get("maintenance_reminder_lead_days"))
    except (TypeError, ValueError):
        return None, "Maintenance reminder lead time must be a whole number of days"
    if not (0 <= lead_days <= 365):
        return None, "Maintenance reminder lead time must be between 0 and 365 days"

    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'default_maintenance_interval_days'", (str(interval_days),)
    )
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'maintenance_reminder_lead_days'", (str(lead_days),)
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_maintenance_settings",
        target_table="app_settings",
        details=f"Set default_maintenance_interval_days={interval_days}, maintenance_reminder_lead_days={lead_days}",
    )

    return {
        "message": "Maintenance settings updated successfully",
        "default_maintenance_interval_days": interval_days,
        "maintenance_reminder_lead_days": lead_days,
    }, None


def update_damage_restriction(conn, data, acting_staff_id):
    """Mag-set sa damage_restriction_threshold app_setting (kutob pila ka
    damaged returns before ma-restrict ang member)."""
    try:
        threshold = int(data.get("damage_restriction_threshold"))
    except (TypeError, ValueError):
        return None, "Damage restriction threshold must be a whole number"
    if not (1 <= threshold <= 50):
        return None, "Damage restriction threshold must be between 1 and 50"

    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'damage_restriction_threshold'", (str(threshold),)
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_damage_restriction_threshold",
        target_table="app_settings",
        details=f"Set damage_restriction_threshold={threshold}",
    )

    return {
        "message": "Damage restriction threshold updated successfully",
        "damage_restriction_threshold": threshold,
    }, None


def update_feature_flags(conn, new_values, acting_staff_id):
    """Mag-toggle sa feature flags (per-feature on/off), naay log entry ra
    para sa mga flags nga na-change gyud, dili ang tanan."""
    current_flags = get_feature_flags(conn)
    changed = {name: val for name, val in new_values.items() if val != current_flags[name]}
    for name, enabled in changed.items():
        key, _label = FEATURE_FLAGS[name]
        conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", ("1" if enabled else "0", key))
    conn.commit()

    for name, enabled in changed.items():
        _key, label = FEATURE_FLAGS[name]
        log_action(
            conn=conn,
            staff_id=acting_staff_id,
            action="enabled_feature" if enabled else "disabled_feature",
            target_table="app_settings",
            target_name=label,
            details=(
                f"{label} was turned on - its tab/routes are visible and usable again for everyone"
                if enabled
                else f"{label} was turned off - its nav link is hidden and its routes/actions are blocked for everyone"
            ),
        )

    return {"message": "Feature access updated", "feature_flags": new_values}


# # # ALARM SETTINGS ---------------------------------------------------------


def update_alarm_settings(conn, data, acting_staff_id):
    """Mag-mute/unmute sa RFID exit-monitoring alarm mismo - independent ni
    sa "monitoring" feature flag, nga naga-gate sa tibuok tab/pipeline
    instead. Tan-awa services/monitoring_service.py's record_rfid_event kung
    unsa gyud ang gina-change ani (walay hunong ang logging/classifying
    bisan asa)."""
    enabled = bool(data.get("enabled"))
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'alarm_enabled'", ("1" if enabled else "0",))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="enabled_alarm" if enabled else "disabled_alarm",
        target_table="app_settings",
        details=(
            "RFID exit-monitoring alarm turned on"
            if enabled
            else "RFID exit-monitoring alarm turned off - detections are still logged, but won't raise the banner/active-alarm state"
        ),
    )

    return {"message": "Alarm settings updated", "enabled": enabled}, None


# # # BARCODE ID PREFIXES -----------------------------------------------------


_PREFIX_SETTINGS = {
    "asset_id_prefix": "Asset ID prefix",
    "member_code_prefix": "Member code prefix",
    "staff_code_prefix": "Staff code prefix",
}


def update_barcode_prefixes(conn, data, acting_staff_id, force_regenerate=False):
    """By default, ang changes kay mag-apply ra sa codes nga i-generate gikan
    ani nga point pasunod - ang existing asset_id/member_code/staff_code
    values (ug ang barcode_ids nga default ana) magpabilin sa ilang existing
    prefix. Tan-awa settings_service.py's get_asset_id_prefix/
    get_member_code_prefix/get_staff_code_prefix ug ang ilang callers sa
    asset_service.py/member_service.py/staff_service.py/setup_service.py.

    `force_regenerate=True` additionally mag-rewrite sa matag existing
    record's own code (tan-awa _force_regenerate_prefixed_codes) - opt-in ug
    password-gated sa route layer, kay kini ra ang part ani nga action nga
    dili trivially reversible (ang old printed barcode labels mo-stop na
    mag-scan sa tama nga record)."""
    prefixes = {key: (data.get(key) or "").strip().upper() for key in _PREFIX_SETTINGS}

    for key, value in prefixes.items():
        if not re.match(BARCODE_PREFIX_PATTERN, value):
            return None, f"{_PREFIX_SETTINGS[key]} must be 2-6 characters: uppercase letters, numbers, or - _ . #"

    for key, value in prefixes.items():
        conn.execute("UPDATE app_settings SET value = ? WHERE key = ?", (value, key))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_barcode_prefixes",
        target_table="app_settings",
        details=(
            f"Set asset_id_prefix={prefixes['asset_id_prefix']}, member_code_prefix={prefixes['member_code_prefix']}, "
            f"staff_code_prefix={prefixes['staff_code_prefix']}"
            + (
                " - only affects codes generated from now on, not existing records"
                if not force_regenerate
                else " - also regenerating every existing record's code (see next log entry)"
            )
        ),
    )

    regenerated = None
    if force_regenerate:
        regenerated = _force_regenerate_prefixed_codes(conn, prefixes, acting_staff_id)

    result = {"message": "Barcode ID prefixes updated", **prefixes}
    if regenerated is not None:
        result["regenerated"] = regenerated
    return result, None


def _force_regenerate_prefixed_codes(conn, prefixes, acting_staff_id):
    """Mag-rewrite sa matag asset/member/staff row's own generated code
    (asset_id/member_code/staff_code) para gamiton ang prefix nga bag-o lang
    na-save, keeping ang existing numeric id suffix sa matag row (same
    `{prefix}{id:05d}` shape nga gina-gamit sa creation - tan-awa
    create_asset/create_member/create_staff). Ang row's barcode_id
    ma-carry along ra kung kini currently equal sa row's own code (i.e. wala
    pa na-customize, per normalize_barcode_id's convention) - ang
    manually-set/scanned custom barcode kay untouched gihapon bisan asa.

    Best-effort per table: usa ka row nga mo-collide sa uban (astronomically
    unlikely - id-derived man ang new codes ug unique ang ids, pero
    theoretically pwede mag-equal ang hand-entered custom barcode) kay
    gina-skip lang ug i-count, dili mag-abort sa tibuok batch."""
    counts = {}

    for table, code_col, prefix_key in (
        ("assets", "asset_id", "asset_id_prefix"),
        ("members", "member_code", "member_code_prefix"),
        ("staff", "staff_code", "staff_code_prefix"),
    ):
        new_prefix = prefixes[prefix_key]
        has_barcode_col = table in ("assets", "members")
        rows = conn.execute(f"SELECT id, {code_col}{', barcode_id' if has_barcode_col else ''} FROM {table}").fetchall()

        updated = 0
        for row in rows:
            new_code = f"{new_prefix}{row['id']:05d}"
            if row[code_col] == new_code:
                continue  # already matches - nothing to do, don't touch barcode_id either
            try:
                if has_barcode_col:
                    new_barcode = new_code if row["barcode_id"] == row[code_col] else row["barcode_id"]
                    conn.execute(
                        f"UPDATE {table} SET {code_col} = ?, barcode_id = ? WHERE id = ?",
                        (new_code, new_barcode, row["id"]),
                    )
                else:
                    conn.execute(f"UPDATE {table} SET {code_col} = ? WHERE id = ?", (new_code, row["id"]))
                updated += 1
            except sqlite3.IntegrityError as e:
                logger.error(f"IntegrityError regenerating {table}.{code_col} for id={row['id']}: {e}")

        conn.commit()
        counts[table] = updated

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="force_regenerated_prefixed_codes",
        target_table="app_settings",
        details=(
            f"Regenerated existing codes to new prefixes: {counts['assets']} asset(s), "
            f"{counts['members']} member(s), {counts['staff']} staff record(s)"
        ),
    )

    return counts


# # # BARCODE LABEL ACTIVITY LOGGING ---------------------------------------


def _describe_barcode_selection(asset_items, member_items):
    """Mag-convert sa duha ka item lists ngadto sa human-readable audit_log
    detail string nga gina-log sa duha ka endpoints."""
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


def split_barcode_items(items):
    """Mag-separate sa mixed items list ngadto sa asset_items ug member_items,
    based sa `type` field."""
    asset_items = [i for i in items if i.get("type") == "asset"]
    member_items = [i for i in items if i.get("type") == "member"]
    return asset_items, member_items


def log_barcode_generate(items, acting_staff_id):
    """Audit-log entry para sa label preview generation (walay download pa)."""
    asset_items, member_items = split_barcode_items(items)
    log_action(
        staff_id=acting_staff_id,
        action="generated_barcode_labels",
        details=f"Generated a label preview for {_describe_barcode_selection(asset_items, member_items)}",
    )


def log_barcode_download(items, size, acting_staff_id):
    """Audit-log entry para sa actual PDF download sa barcode labels."""
    asset_items, member_items = split_barcode_items(items)
    log_action(
        staff_id=acting_staff_id,
        action="downloaded_barcode_labels",
        details=f"Downloaded a {size} PDF for {_describe_barcode_selection(asset_items, member_items)}",
    )


_BARCODE_COUNT_RE = re.compile(r"(\d+) (?:asset|member)\(s\)")


def get_barcode_labels_printed_count(conn):
    """Total individual labels ever na-download (System page's Hardware card
    summary) - gina-sum ang asset(s)/member(s) counts gikan sa matag
    downloaded_barcode_labels audit_log entry's own details string
    (log_barcode_download sa taas) instead of tracking og separate counter,
    kay naa naman diay sa audit log ang numbers nga na-record sa download."""
    rows = conn.execute(
        "SELECT details FROM audit_log WHERE action = 'downloaded_barcode_labels'"
    ).fetchall()
    return sum(
        int(n) for row in rows for n in _BARCODE_COUNT_RE.findall(row["details"] or "")
    )


# # # ONLINE STAFF ----------------------------------------------------------


def get_online_staff(conn, roles=None):
    """Staff nga naay currently-live session, same definition sa
    require_session; ang `roles` mag-narrow sa set kung gi-supply."""
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


# # # DATABASE BACKUPS -------------------------------------------------------


def list_backups():
    """Newest-first list sa backup files on disk; empty list ra, dili error,
    kung wala pa gyuy na-run nga first backup."""
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


def get_backup_settings_context(conn):
    """Everything nga gikinahanglan sa System page's Backup card sa usa ka call."""
    return {
        "enabled": get_auto_backup_enabled(conn),
        "interval_hours": get_auto_backup_interval_hours(conn),
        "last_backup_at": get_last_backup_at(conn),
        "backups": list_backups(),
        "keep_last_n": backup_db.KEEP_LAST_N,
    }


def update_backup_settings(conn, data, acting_staff_id):
    """Mag-toggle sa auto-backup on/off ug mag-set sa interval_hours."""
    enabled = bool(data.get("enabled"))
    try:
        interval_hours = int(data.get("interval_hours"))
    except (TypeError, ValueError):
        return None, "interval_hours must be a whole number"
    if interval_hours < 1 or interval_hours > 168:
        return None, "interval_hours must be between 1 and 168 (one week)"

    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'auto_backup_enabled'", ("1" if enabled else "0",)
    )
    conn.execute(
        "UPDATE app_settings SET value = ? WHERE key = 'auto_backup_interval_hours'", (str(interval_hours),)
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_backup_settings",
        target_table="app_settings",
        details=f"Automatic backups {'enabled' if enabled else 'disabled'}, every {interval_hours}h",
    )

    return {"message": "Backup settings updated", "enabled": enabled, "interval_hours": interval_hours}, None


def trigger_manual_backup(acting_staff_id):
    """Manual backup; mo-run gyud unconditionally regardless sa enabled
    toggle or sa schedule interval. Mo-run sa background thread (dili
    mo-block sa request) - naay dakong DB o hinay nga SD card mahimong
    modugay pa gyud ang backup, so dili maayo hulaton dayon sa parehas nga
    request/thread. Mo-balik dayon "started"; ang frontend mag-poll sa
    manage-form endpoint para makita kung human na (last_backup_at
    nausab)."""
    def _run_in_background():
        ok = run_backup_now()
        if ok:
            log_action(
                staff_id=acting_staff_id,
                action="ran_manual_backup",
                target_table="app_settings",
                details="Triggered an on-demand database backup from the System page",
            )
        else:
            logger.warning(f"Manual backup (triggered by staff_id={acting_staff_id}) failed.")

    threading.Thread(target=_run_in_background, daemon=True).start()
    return {"message": "Backup started", "started": True}, None


def resolve_backup_path(filename):
    """Nag-validate sa filename shape + path containment (dili makagawas sa
    backup dir).

    Returns: (path, error_msg, status)."""
    if not _BACKUP_FILENAME_RE.match(filename):
        return None, "Invalid backup filename", 400

    path = os.path.realpath(os.path.join(backup_db.DEFAULT_BACKUP_DIR, filename))
    backup_dir_real = os.path.realpath(backup_db.DEFAULT_BACKUP_DIR)
    # Belt-and-suspenders on the regex: confirms the resolved path actually lands inside the backup dir.
    if not path.startswith(backup_dir_real + os.sep) or not os.path.isfile(path):
        return None, "Backup not found", 404

    return path, None, None


def delete_backup_file(filename, acting_staff_id):
    """Mag-delete sa usa ka backup file on disk human ma-validate ang filename/path."""
    path, error_msg, status = resolve_backup_path(filename)
    if error_msg:
        return None, error_msg, status

    os.remove(path)

    log_action(
        staff_id=acting_staff_id,
        action="deleted_backup",
        target_table="app_settings",
        details=f"Deleted database backup file: {filename}",
    )
    return list_backups(), None, None


# # # ARCHIVE AUTO-PURGE --------------------------------------------------------


def archived_record_counts(conn):
    """{assets, members, staff} counts nga currently archived/deactivated,
    gina-pakita before mag-set og retention window."""
    assets = conn.execute("SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NOT NULL").fetchone()["cnt"]
    members = conn.execute("SELECT COUNT(*) AS cnt FROM members WHERE is_active = 0").fetchone()["cnt"]
    staff = conn.execute("SELECT COUNT(*) AS cnt FROM staff WHERE is_active = 0").fetchone()["cnt"]
    return {"assets": assets, "members": members, "staff": staff}


def update_archive_purge_settings(conn, data, acting_staff_id):
    """Mag-set sa archive_purge_days app_setting (0 = None/disabled, max 3650)."""
    try:
        purge_days = int(data.get("purge_days"))
    except (TypeError, ValueError):
        return None, "purge_days must be a whole number"
    # 0 = "None" (disabled); 3650 (10 years) caps the custom input against a fat-fingered "never".
    if purge_days < 0 or purge_days > 3650:
        return None, "purge_days must be between 0 (None) and 3650"

    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'archive_purge_days'", (str(purge_days),))
    conn.commit()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="updated_archive_purge_settings",
        target_table="app_settings",
        details=(
            f"Auto-purge of archived assets/deactivated members & staff set to {purge_days} day(s)"
            if purge_days > 0
            else "Auto-purge of archived assets/deactivated members & staff turned off (None)"
        ),
    )

    return {"message": "Auto-purge settings updated", "purge_days": purge_days}, None


def run_archive_purge(conn, acting_staff_id):
    """Manual purge; mo-run sa same three passes sa scheduled job, pero only
    kung naay na-set nga retention window."""
    purge_days = get_archive_purge_days(conn)
    if purge_days <= 0:
        return None, "Set a retention window before purging - it's currently set to None."

    purged_assets = purge_expired_archived_assets()
    purged_staff = purge_expired_inactive_staff()
    purged_members = purge_expired_inactive_members()

    log_action(
        conn=conn,
        staff_id=acting_staff_id,
        action="ran_manual_archive_purge",
        target_table="assets",
        details=(
            "Triggered an on-demand archive purge from the System page - "
            f"{purged_assets} asset(s), {purged_members} member(s), {purged_staff} staff account(s) permanently deleted"
        ),
    )
    return {
        "message": f"Purged {purged_assets} asset(s), {purged_members} member(s), {purged_staff} staff account(s)",
        "purged_assets": purged_assets,
        "purged_members": purged_members,
        "purged_staff": purged_staff,
    }, None
