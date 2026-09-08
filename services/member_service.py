"""Query-building ug mutation logic para sa routes/members.py (members + departments).

Same convention sa services/asset_service.py: matag function mo-dawat ug bukas nga
`conn` (ang route ang mo-open/close), mobalik ug `(data, error)`. `error` None kung
malampuson, o `(error_payload, status, context)` kung napakyas - `error_payload` plain
dict (para sa jsonify), `status` ang HTTP status, ug `context` dict sa dugang objects
(e.g. {"member": member}) nga gikinahanglan sa route para i-rerender ang inline-error
template sa non-JSON nga request. `context` `{}` kung wala'y i-rerender.
"""

import csv
import io
import logging
import os
from datetime import datetime

from database import get_db_connection, sqlite3
from services.settings_service import get_member_code_prefix
from utils.exports import sanitize_export_filename
from utils.formatters import full_name
from utils.logger import log_action
from utils.validators import normalize_barcode_id

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)

VALID_MEMBER_TYPES = ("trainee", "trainer")

# Flat cap instead of real pagination - see get_restriction_history docstring.
RESTRICTION_HISTORY_CAP = 20

# "name" maps to last_name + first_name (no single full-name column), both sorted the same direction.
SORTABLE_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "member_code": "members.member_code",
    "department": "departments.name",
    "member_type": "members.member_type",
    "barcode_id": "members.barcode_id",
    "batch": "members.batch",
}

# No exclusions needed - unlike staff, members have no password/credential fields.
MEMBERS_EXPORT_COLUMNS = (
    "member_code",
    "first_name",
    "last_name",
    "middle_name",
    "suffix",
    "barcode_id",
    "batch",
    "department",
    "member_type",
    "is_active",
    "is_restricted",
    "restriction_reason",
    "created_at",
)

MEMBERS_EXPORT_FIELD_LABELS = {
    "member_code": "Member Code",
    "first_name": "First Name",
    "last_name": "Last Name",
    "middle_name": "Middle Name",
    "suffix": "Suffix",
    "barcode_id": "Barcode ID",
    "batch": "Batch",
    "department": "Department",
    "member_type": "Member Type",
    "is_active": "Active",
    "is_restricted": "Restricted",
    "restriction_reason": "Restriction Reason",
    "created_at": "Created At",
}

REQUIRED_MEMBER_CSV_COLUMNS = ("first_name", "last_name", "department", "member_type")
OPTIONAL_MEMBER_CSV_COLUMNS = ("middle_name", "suffix", "batch")


def _no_ctx(error_payload, status):
    """Shortcut para sa error returns nga wala'y extra context - `(payload, status, {})`."""
    return (error_payload, status, {})


# # # LOOKUPS shared by several handlers -----------------------------------


def list_departments(conn):
    """Tanan departments, sorted by sort_order tapos name - gamit sa dropdowns."""
    return conn.execute("SELECT * FROM departments ORDER BY sort_order, name").fetchall()


# # # LIST / VIEW ------------------------------------------------------------


def get_members_table_context(conn, args, limit, offset):
    """Shared query para sa members table, gamit sa htmx partial ug sa full-page route -
    naay filter (search, department, is_restricted), sortable columns, ug pagination."""
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

    # Truthiness check on purpose: "" from the unset filter would else read as is_restricted=0.
    is_restricted = args.get("is_restricted")
    if is_restricted:
        if is_restricted.lower() not in ("0", "1", "true", "false"):
            return None, _no_ctx({"error": "is_restricted must be '0' or '1'"}, 400)
        where_clauses.append("members.is_restricted = ?")
        params.append(1 if is_restricted.lower() in ("1", "true") else 0)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    sort_by = args.get("sort_by", "name")
    if sort_by not in SORTABLE_COLUMNS:
        return None, _no_ctx(
            {"error": f"sort_by must be one of: {', '.join(SORTABLE_COLUMNS)}"}, 400
        )
    sort_dir = args.get("sort_dir", "asc").lower()
    if sort_dir not in ("asc", "desc"):
        return None, _no_ctx({"error": "sort_dir must be 'asc' or 'desc'"}, 400)

    # sort_column may be several comma-separated columns; apply the direction to all of them.
    dir_sql = sort_dir.upper()
    order_by_sql = ", ".join(
        f"{col.strip()} {dir_sql}" for col in SORTABLE_COLUMNS[sort_by].split(",")
    )

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

    return {
        "members": members,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_inactive": show_inactive,
        "sort_by": sort_by,
        "sort_dir": sort_dir,
    }, None


def get_member_stat_cards(conn):
    """Ang 4 ka numero sa Members page's stat cards (All/Active/Restricted/Inactive).

    Returns: dict {stat_all, stat_active, stat_restricted, stat_inactive}."""
    stat_all = conn.execute("SELECT COUNT(*) AS cnt FROM members").fetchone()["cnt"]
    stat_active = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 1 AND is_restricted = 0"
    ).fetchone()["cnt"]
    stat_restricted = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 1 AND is_restricted = 1"
    ).fetchone()["cnt"]
    stat_inactive = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE is_active = 0"
    ).fetchone()["cnt"]
    return {
        "stat_all": stat_all,
        "stat_active": stat_active,
        "stat_restricted": stat_restricted,
        "stat_inactive": stat_inactive,
    }


def find_member_id_by_barcode(conn, barcode):
    """Exact-match barcode lookup para sa search box's Enter key; ma-match gihapon
    bisan deactivated na ang member."""
    row = conn.execute(
        "SELECT id FROM members WHERE UPPER(barcode_id) = UPPER(?)", (barcode,)
    ).fetchone()
    return row["id"] if row else None


def get_member_profile(conn, member_id):
    """Read-only profile modal data; ang uban (transactions/history) mo-load client-side
    via separate calls. Kung restricted ang member, gi-apil dinhi ang pending
    unrestriction request para dili na mangita pa ang staff sa Approvals."""
    member = conn.execute(
        """
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE members.id = ?
        """,
        (member_id,),
    ).fetchone()

    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    # Surfaces a pending unrestriction request here, so staff don't have to dig through Approvals.
    pending_unrestriction_request = None
    if member["is_restricted"]:
        pending_unrestriction_request = conn.execute(
            """
            SELECT unrestriction_requests.id, unrestriction_requests.reason,
                   unrestriction_requests.created_at, staff.username AS requested_by_username
            FROM unrestriction_requests
            JOIN staff ON unrestriction_requests.requested_by_staff_id = staff.id
            WHERE unrestriction_requests.member_id = ? AND unrestriction_requests.status = 'pending'
            """,
            (member_id,),
        ).fetchone()

    return {"member": member, "pending_unrestriction_request": pending_unrestriction_request}, None


# # # CREATE ------------------------------------------------------------


def create_member(conn, data, staff_id):
    """Mag-insert ug bag-ong member. Ang member_code kanunay auto-generate as MBR##### -
    ang barcode_id parehas ana gawas kung naay gi-supply na custom barcode (e.g. scanned
    off sa existing school/employee ID sa member), diha gamiton na hinuon na kay dili na
    mag-print og bag-ong barcode."""
    error = _validate_department_id(conn, data.get("department_id"))
    if error:
        return None, _no_ctx(*error)

    if data.get("member_type") not in VALID_MEMBER_TYPES:
        return None, _no_ctx(
            {"error": f"member_type must be one of: {', '.join(VALID_MEMBER_TYPES)}"}, 400
        )

    custom_barcode, barcode_error = normalize_barcode_id(data.get("barcode_id"))
    if barcode_error:
        return None, _no_ctx({"error": barcode_error}, 400)

    if custom_barcode is not None:
        existing = conn.execute(
            "SELECT id FROM members WHERE UPPER(barcode_id) = UPPER(?)", (custom_barcode,)
        ).fetchone()
        if existing:
            return None, _no_ctx(
                {"error": "This barcode is already registered to another member."}, 400
            )

    try:
        cursor = conn.execute(
            """
            INSERT INTO members (first_name, last_name, middle_name, suffix, barcode_id, batch, department_id, member_type, phone_number)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data["first_name"],
                data["last_name"],
                data.get("middle_name"),
                data.get("suffix"),
                "",
                data.get("batch"),
                data["department_id"],
                data["member_type"],
                data.get("phone_number"),
            ),
        )
        new_member_id = cursor.lastrowid
        generated_code = f"{get_member_code_prefix(conn)}{new_member_id:05d}"
        conn.execute(
            "UPDATE members SET member_code = ?, barcode_id = ? WHERE id = ?",
            (generated_code, custom_barcode or generated_code, new_member_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        if custom_barcode is not None:
            return None, _no_ctx(
                {"error": "This barcode is already registered to another member."}, 400
            )
        return None, _no_ctx(
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
        )

    name = full_name(data['first_name'], data['last_name'], data.get('middle_name'), data.get('suffix'))
    log_action(
        staff_id=staff_id,
        action="created_member",
        target_table="members",
        target_id=new_member_id,
        target_name=name,
        details=f"Created member {name}",
    )

    return {"id": new_member_id, "member_code": generated_code}, None


def _validate_department_id(conn, department_id, required=False):
    """Local, non-Flask mirror sa utils.validators.validate_lookup_id(conn, "departments", ...)
    - parehas ra ang behavior/wording, mobalik lang ug plain (dict, status) imbes og
    jsonify response."""
    if department_id is None:
        if required:
            return {"error": "Missing required field: department_id"}, 400
        return None
    row = conn.execute("SELECT id FROM departments WHERE id = ?", (department_id,)).fetchone()
    if row is None:
        return {"error": "department_id does not reference an existing entry"}, 400
    return None


# # # UPDATE ------------------------------------------------------------


def get_member_for_edit(conn, member_id):
    """Fetch member para sa edit modal/form; 404 kung wala."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def update_member(conn, member_id, data, staff_id, acting_staff_member_id):
    """Mag-update sa member details - naay separate validation para sa barcode
    reuse/uniqueness ug sa is_restricted toggle (dili pwede mag-unrestrict sa
    kaugalingon account). Mag-log ug restricted_member/unrestricted_member kung
    nabag-o ang restriction status."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    if "department_id" in data:
        error = _validate_department_id(conn, data.get("department_id"))
        if error:
            # Matches the original route: this specific failure always returns raw JSON,
            # even on non-JSON (htmx/form) requests - no modal re-render, unlike the other errors below.
            return None, _no_ctx(*error)

    new_barcode_id = member["barcode_id"]
    if "barcode_id" in data:
        custom_barcode, barcode_error = normalize_barcode_id(data.get("barcode_id"))
        if barcode_error:
            return None, ({"error": barcode_error}, 400, {"member": member})
        # Blank clears back to nothing meaningful to fall back to - barcode_id is
        # NOT NULL, so refuse rather than silently keeping the old value or erroring
        # obscurely at the UPDATE.
        if custom_barcode is None:
            return None, (
                {"error": "barcode_id cannot be blank - clear it back to the member code instead if needed."},
                400,
                {"member": member},
            )
        if custom_barcode.upper() != (member["barcode_id"] or "").upper():
            existing = conn.execute(
                "SELECT id FROM members WHERE UPPER(barcode_id) = UPPER(?) AND id != ?",
                (custom_barcode, member_id),
            ).fetchone()
            if existing:
                return None, (
                    {"error": "This barcode is already registered to another member."},
                    400,
                    {"member": member},
                )
        new_barcode_id = custom_barcode

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
        and acting_staff_member_id == member_id
    ):
        return None, (
            {
                "error": "You cannot unrestrict your own account.",
                "reason": "Ask another admin or in_charge to review it.",
            },
            403,
            {"member": member},
        )

    try:
        conn.execute(
            """
            UPDATE members
            SET first_name = ?, last_name = ?, middle_name = ?, suffix = ?, department_id = ?, batch = ?, phone_number = ?, barcode_id = ?, is_restricted = ?, restriction_reason = ?, restriction_source = ?
            WHERE id = ?
            """,
            (
                data.get("first_name", member["first_name"]),
                data.get("last_name", member["last_name"]),
                data.get("middle_name", member["middle_name"]),
                data.get("suffix", member["suffix"]),
                data.get("department_id", member["department_id"]),
                data.get("batch", member["batch"]),
                data.get("phone_number", member["phone_number"]),
                new_barcode_id,
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
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"member": member},
        )

    updated_name = full_name(
        data.get("first_name", member["first_name"]),
        data.get("last_name", member["last_name"]),
        data.get("middle_name", member["middle_name"]),
        data.get("suffix", member["suffix"]),
    )
    log_action(
        staff_id=staff_id,
        action="updated_member",
        target_table="members",
        target_id=member["id"],
        target_name=updated_name,
        details=f"Updated member {updated_name}",
    )

    if old_is_restricted != new_is_restricted:
        if new_is_restricted:
            log_action(
                staff_id=staff_id,
                action="restricted_member",
                target_table="members",
                target_id=member["id"],
                target_name=updated_name,
                details=f"Restricted member {updated_name}: {data.get('restriction_reason', 'No reason given')}",
            )
        else:
            log_action(
                staff_id=staff_id,
                action="unrestricted_member",
                target_table="members",
                target_id=member["id"],
                target_name=updated_name,
                details=f"Lifted restriction on member {updated_name}",
            )

    return {"message": "Member updated successfully"}, None


# # # RESTRICTION (manual) ------------------------------------------------


def get_member_for_restrict(conn, member_id):
    """Fetch member para sa restrict modal; 404 kung wala."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def restrict_member(conn, member_id, restriction_reason, staff_id):
    """Bisan kinsa nga staff pwede mag-flag ug member diretso with a reason - low-risk ug
    sayon i-trigger, pero naka-log gihapon."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    if not restriction_reason:
        return None, ({"error": "Missing required field: restriction_reason"}, 400, {"member": member})

    if member["is_restricted"]:
        return None, ({"error": "Member is already restricted"}, 400, {"member": member})

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'manual' WHERE id = ?",
            (restriction_reason, member_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"member": member},
        )

    updated_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])
    log_action(
        staff_id=staff_id,
        action="restricted_member",
        target_table="members",
        target_id=member_id,
        target_name=updated_name,
        details=f"Restricted member {updated_name}: {restriction_reason}",
    )

    return {"message": "Member restricted successfully", "name": updated_name}, None


def get_member_for_unrestrict(conn, member_id):
    """Fetch member para sa unrestrict action; 404 kung wala."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def unrestrict_member(conn, member_id, staff_id, acting_staff_member_id):
    """Ang pag-lift sa restriction mo-balik sa borrowing access, so mas dako og
    authority ang gikinahanglan kaysa sa pag-restrict - dili ni ma-call sa tool_keeper.
    Dili sad pwede mag-unrestrict sa kaugalingon account."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    if not member["is_restricted"]:
        return None, ({"error": "Member is not currently restricted"}, 400, {"member": member})

    if acting_staff_member_id == member_id:
        return None, (
            {
                "error": "You cannot unrestrict your own account.",
                "reason": "Ask another admin or in_charge to review it.",
            },
            403,
            {"member": member},
        )

    try:
        conn.execute(
            "UPDATE members SET is_restricted = 0, restriction_reason = NULL, restriction_source = NULL WHERE id = ?",
            (member_id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"member": member},
        )

    updated_name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])
    log_action(
        staff_id=staff_id,
        action="unrestricted_member",
        target_table="members",
        target_id=member_id,
        target_name=updated_name,
        details=f"Lifted restriction on member {updated_name}",
    )

    return {"message": "Member unrestricted successfully"}, None


def get_member_for_unrestriction_request(conn, member_id):
    """Modal sa tool_keeper para i-flag ug restricted member for review; ang admin/in_charge
    diretso ra mag-lift sa restriction, dili na kinahanglan mo-agi ani."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    if not member["is_restricted"]:
        return None, _no_ctx({"error": "Member is not currently restricted"}, 400)
    return member, None


# # # DELETE (deactivate) -----------------------------------------------


def get_member_for_delete(conn, member_id):
    """Fetch member para sa deactivate modal; 404 kung wala."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def deactivate_member(conn, member_id, staff_id, acting_staff_role):
    """Mag-deactivate ug member, pero naay daghang guards: dili pwede kung naay open
    borrow transaction o open checkout session, ug kung naay linked active staff
    account dapat admin ra ang mo-deactivate (labi na kung admin pud tong staff account).
    Mag-cascade deactivate sa linked staff account kung naa."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    if not member["is_active"]:
        return None, ({"error": "Member is already deactivated"}, 400, {"member": member})

    open_loan = conn.execute(
        "SELECT id FROM transactions WHERE member_id = ? AND status = 'borrowed' LIMIT 1",
        (member_id,),
    ).fetchone()
    if open_loan is not None:
        return None, (
            {
                "error": "Cannot deactivate member: they have an open borrow transaction",
                "reason": "This member currently has one or more tools checked out. They must be returned first, or contact an admin.",
            },
            409,
            {"member": member},
        )

    open_session = conn.execute(
        "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
        (member_id,),
    ).fetchone()
    if open_session is not None:
        return None, (
            {
                "error": "Cannot deactivate member: they have an open checkout session",
                "reason": "This member has tools scanned into an unfinished checkout. Finish or cancel that session first, or contact an admin.",
            },
            409,
            {"member": member},
        )

    linked_staff = conn.execute(
        "SELECT * FROM staff WHERE member_id = ? AND is_active = 1", (member_id,)
    ).fetchone()

    if linked_staff is not None and acting_staff_role != "admin":
        return None, (
            {
                "error": "Cannot deactivate member: they have an active staff account",
                "reason": "This member is also active staff. Only an admin can deactivate them.",
            },
            403,
            {"member": member},
        )

    if linked_staff is not None and linked_staff["role"] == "admin":
        return None, (
            {
                "error": "Cannot deactivate member: they have an active admin account",
                "reason": "Admin accounts cannot be deactivated this way - use /staff/<id>/request-deactivation instead, which requires peer approval.",
            },
            403,
            {"member": member},
        )

    try:
        conn.execute(
            "UPDATE members SET is_active = 0, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
            (member_id,),
        )
        if linked_staff is not None:
            conn.execute(
                "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
                (linked_staff["id"],),
            )
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"member": member},
        )

    name = full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])
    log_action(
        staff_id=staff_id,
        action="deactivated_member",
        target_table="members",
        target_id=member_id,
        target_name=name,
        details=f"Deactivated member {name}",
    )

    if linked_staff is not None:
        log_action(
            staff_id=staff_id,
            action="deactivated_staff_cascade",
            target_table="staff",
            target_id=linked_staff["id"],
            target_name=linked_staff["username"],
            details=f"Staff account {linked_staff['username']} auto-deactivated because linked member was deactivated",
        )

    return {"message": "Member deactivated successfully", "name": name}, None


def get_member_for_reactivate(conn, member_id):
    """Fetch member para sa reactivate action; 404 kung wala."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def reactivate_member(conn, member_id, staff_id):
    """Mag-reactivate balik sa member ug mag-clear sa deactivated_at."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    if member["is_active"]:
        return None, ({"error": "Member is already active"}, 400, {"member": member})

    try:
        conn.execute("UPDATE members SET is_active = 1, deactivated_at = NULL WHERE id = ?", (member_id,))
        conn.commit()
    except sqlite3.IntegrityError as e:
        conn.rollback()
        logger.error(f"IntegrityError: {e}")
        return None, (
            {"error": "A database constraint was violated. Check for duplicate barcode/username/ID values."},
            400,
            {"member": member},
        )

    name = full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])
    log_action(
        staff_id=staff_id,
        action="reactivated_member",
        target_table="members",
        target_id=member_id,
        target_name=name,
        details=f"Reactivated member {name}",
    )

    return {"message": "Member reactivated successfully", "name": name}, None


# # # BULK ACTIONS -------------------------------------------------------


def bulk_deactivate_members(conn, ids, staff_id, acting_staff_role):
    """Bulk deactivate ug daghang members dungan - kung naay bad row, mo-adto ra na sa
    `skipped` list, dili mo-fail ang tibuok batch. Parehas ra ka strikto ang guards sa
    single deactivate_member."""
    deactivated, skipped = [], []
    staff_cascades = []  # (staff_id, username) pairs to log after the loop

    for member_id in ids:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

        if member is None:
            skipped.append({"id": member_id, "reason": "Member not found"})
            continue

        name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])

        if not member["is_active"]:
            skipped.append({"id": member_id, "name": name, "reason": "Already deactivated"})
            continue

        open_loan = conn.execute(
            "SELECT id FROM transactions WHERE member_id = ? AND status = 'borrowed' LIMIT 1",
            (member_id,),
        ).fetchone()
        if open_loan is not None:
            skipped.append({"id": member_id, "name": name, "reason": "Currently has one or more tools checked out"})
            continue

        open_session = conn.execute(
            "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
            (member_id,),
        ).fetchone()
        if open_session is not None:
            skipped.append({"id": member_id, "name": name, "reason": "Has an open checkout session"})
            continue

        linked_staff = conn.execute(
            "SELECT * FROM staff WHERE member_id = ? AND is_active = 1", (member_id,)
        ).fetchone()

        if linked_staff is not None and acting_staff_role != "admin":
            skipped.append(
                {"id": member_id, "name": name, "reason": "Has an active staff account - only an admin can deactivate"}
            )
            continue

        if linked_staff is not None and linked_staff["role"] == "admin":
            skipped.append(
                {"id": member_id, "name": name, "reason": "Has an active admin account - deactivate via staff management instead"}
            )
            continue

        try:
            conn.execute(
                "UPDATE members SET is_active = 0, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
                (member_id,),
            )
            if linked_staff is not None:
                conn.execute(
                    "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
                    (linked_staff["id"],),
                )
            conn.commit()
            deactivated.append({"id": member_id, "name": name})
            if linked_staff is not None:
                staff_cascades.append((linked_staff["id"], linked_staff["username"]))
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk deactivate of member {member_id}: {e}")
            skipped.append({"id": member_id, "name": name, "reason": "A database constraint was violated"})

    if deactivated:
        log_action(
            staff_id=staff_id,
            action="bulk_deactivated_members",
            target_table="members",
            details=(
                f"Bulk deactivated {len(deactivated)} member(s): "
                + ", ".join(m["name"] for m in deactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    for cascade_staff_id, username in staff_cascades:
        log_action(
            staff_id=staff_id,
            action="deactivated_staff_cascade",
            target_table="staff",
            target_id=cascade_staff_id,
            details=f"Staff account {username} auto-deactivated because linked member was bulk-deactivated",
        )

    return {
        "deactivated_count": len(deactivated),
        "deactivated": deactivated,
        "skipped": skipped,
    }, None


def bulk_reactivate_members(conn, ids, staff_id):
    """Bulk reactivate, open ra sa admin/in_charge; mas gaan ni na gate kumpara sa
    asset restore nga admin-only jud. Bad row mo-adto sa `skipped`, dili mo-fail
    ang tibuok batch."""
    reactivated, skipped = [], []

    for member_id in ids:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

        if member is None:
            skipped.append({"id": member_id, "reason": "Member not found"})
            continue

        name = full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])

        if member["is_active"]:
            skipped.append({"id": member_id, "name": name, "reason": "Already active"})
            continue

        try:
            conn.execute("UPDATE members SET is_active = 1, deactivated_at = NULL WHERE id = ?", (member_id,))
            conn.commit()
            reactivated.append({"id": member_id, "name": name})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk reactivate of member {member_id}: {e}")
            skipped.append({"id": member_id, "name": name, "reason": "A database constraint was violated"})

    if reactivated:
        log_action(
            staff_id=staff_id,
            action="bulk_reactivated_members",
            target_table="members",
            details=(
                f"Bulk reactivated {len(reactivated)} member(s): "
                + ", ".join(m["name"] for m in reactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    return {
        "reactivated_count": len(reactivated),
        "reactivated": reactivated,
        "skipped": skipped,
    }, None


# # # API ROUTES (scanning lookups) ------------------------------------------


def get_member_by_barcode(conn, barcode):
    """Barcode scan lookup gamit sa checkout/API flows; mobalik ug plain dict (dili
    sqlite3.Row) para JSON-serializable dayon."""
    member = conn.execute(
        """
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE UPPER(members.barcode_id) = UPPER(?)
        """,
        (barcode,),
    ).fetchone()

    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return dict(member), None


def get_member_transactions(conn, member_id, limit, offset, status_filter):
    """Member's transaction history, filterable by status (current/returned/tanan),
    gamit sa profile modal - paginated pero naay fixed small default limit."""
    member = conn.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    where_clause = "WHERE transactions.member_id = ?"
    params = [member_id]

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in transactions],
    }, None


def get_borrow_history_modal_member(conn, member_id):
    """"View Full History" shell para sa Borrow History; ang rows mo-load gikan sa
    partial via htmx."""
    member = conn.execute(
        """
        SELECT members.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM members WHERE members.id = ?
        """,
        (member_id,),
    ).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def get_borrow_history_table(conn, member_id, limit, offset):
    """Parehas ra sa query sa get_member_transactions pero real pagination ni, dili
    fixed limit=3."""
    member = conn.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE member_id = ?", (member_id,)
    ).fetchone()["cnt"]

    transactions = conn.execute(
        """
        SELECT transactions.*, assets.name AS asset_name, assets.asset_id AS asset_code
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        WHERE transactions.member_id = ?
        ORDER BY transactions.time_out DESC
        LIMIT ? OFFSET ?
        """,
        (member_id, limit, offset),
    ).fetchall()

    return {
        "transactions": transactions,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }, None


def _fetch_restriction_history(member_id, conn):
    """Shared query nga mag-combine sa restrict/unrestrict audit events ug sa
    unrestriction_requests trail; naay cap (RESTRICTION_HISTORY_CAP), dili paginated."""
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
        LIMIT ?
        """,
        (member_id, RESTRICTION_HISTORY_CAP),
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
        LIMIT ?
        """,
        (member_id, RESTRICTION_HISTORY_CAP),
    ).fetchall()

    return restriction_events, unrestriction_requests


def get_restriction_history(conn, member_id):
    """JSON teaser para sa Restriction History tab; open ni sa bisan kinsang staff,
    tool_keeper apil."""
    member = conn.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    restriction_events, unrestriction_requests = _fetch_restriction_history(member_id, conn)

    return {
        "restriction_events": [dict(row) for row in restriction_events],
        "unrestriction_requests": [dict(row) for row in unrestriction_requests],
        "capped_at": RESTRICTION_HISTORY_CAP,
    }, None


def get_restriction_history_modal_member(conn, member_id):
    """"View Full History" shell para sa Restriction History; ang rows mo-load gikan
    sa partial sa ubos."""
    member = conn.execute(
        """
        SELECT members.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM members WHERE members.id = ?
        """,
        (member_id,),
    ).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)
    return member, None


def get_restriction_history_table(conn, member_id, limit, offset):
    """Paginated htmx partial; ang audit_log ug unrestriction_requests gi-UNION ALL
    para mahimong usa ka timeline."""
    member = conn.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None:
        return None, _no_ctx({"error": "Member not found"}, 404)

    union_sql = """
        SELECT 'event' AS kind, audit_log.action AS title, audit_log.details AS detail,
               staff.username AS actor, NULL AS reviewer, audit_log.created_at AS created_at
        FROM audit_log
        LEFT JOIN staff ON audit_log.staff_id = staff.id
        WHERE audit_log.target_table = 'members' AND audit_log.target_id = ?
        AND audit_log.action IN (
            'restricted_member', 'unrestricted_member',
            'auto_restricted_member', 'auto_unrestricted_member'
        )
        UNION ALL
        SELECT 'request' AS kind, unrestriction_requests.status AS title, unrestriction_requests.reason AS detail,
               requester.username AS actor, reviewer.username AS reviewer, unrestriction_requests.created_at AS created_at
        FROM unrestriction_requests
        JOIN staff AS requester ON unrestriction_requests.requested_by_staff_id = requester.id
        LEFT JOIN staff AS reviewer ON unrestriction_requests.reviewed_by_staff_id = reviewer.id
        WHERE unrestriction_requests.member_id = ?
    """
    params = (member_id, member_id)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM ({union_sql})", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"{union_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    ).fetchall()

    return {
        "rows": rows,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
    }, None


# # # DEPARTMENTS (lookup table) -----------------------------------------


def get_departments_with_member_count(conn):
    """Tanan departments dungan sa member count per department - gamit sa delete modal
    para mo-warn kung naay members pa assigned."""
    # LEFT JOIN + COUNT gives 0, not NULL, for the delete modal to warn on.
    return conn.execute(
        """
        SELECT departments.*, COUNT(members.id) AS member_count
        FROM departments
        LEFT JOIN members ON members.department_id = departments.id
        GROUP BY departments.id
        ORDER BY departments.sort_order, departments.name
        """
    ).fetchall()


def add_department(conn, name, staff_id):
    """Mag-insert ug bag-ong department; auto-append sa last (highest sort_order).
    Duplicate name mo-error base sa DB unique constraint."""
    name = (name or "").strip()
    if not name:
        return None, _no_ctx({"error": "Missing required field: name"}, 400)

    try:
        # New departments go last; COALESCE handles the first-ever department where MAX is NULL.
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
        return None, _no_ctx({"error": "A department with this name already exists"}, 400)

    log_action(
        staff_id=staff_id,
        action="created_department",
        target_table="departments",
        target_id=new_id,
        target_name=name,
        details=f"Created department: {name}",
    )

    return {"id": new_id, "name": name}, None


def edit_department(conn, department_id, name, staff_id):
    """Mag-rename ug department; duplicate name mo-error base sa DB unique constraint."""
    name = (name or "").strip()
    if not name:
        return None, _no_ctx({"error": "Missing required field: name"}, 400)

    department = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
    if department is None:
        return None, _no_ctx({"error": "Department not found"}, 404)

    try:
        conn.execute("UPDATE departments SET name = ? WHERE id = ?", (name, department_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        return None, _no_ctx({"error": "A department with this name already exists"}, 400)

    log_action(
        staff_id=staff_id,
        action="renamed_department",
        target_table="departments",
        target_id=department_id,
        target_name=name,
        details=f"Renamed department '{department['name']}' to '{name}'",
    )

    return {"message": "Department renamed successfully"}, None


def reorder_departments(conn, ordered_ids, staff_id):
    """Mag-save sa department order; gikinahanglan ang full id set para dili ma-orphan
    ang usa ka department gikan sa partial request."""
    if len(set(ordered_ids)) != len(ordered_ids):
        return None, _no_ctx({"error": "order contains duplicate department ids"}, 400)

    existing_ids = {row["id"] for row in conn.execute("SELECT id FROM departments").fetchall()}

    if set(ordered_ids) != existing_ids:
        return None, _no_ctx({"error": "order must include exactly the current set of departments"}, 400)

    try:
        for position, dept_id in enumerate(ordered_ids):
            conn.execute("UPDATE departments SET sort_order = ? WHERE id = ?", (position, dept_id))
        conn.commit()
    except sqlite3.Error:
        conn.rollback()
        logger.exception("Failed to save department order")
        return None, _no_ctx({"error": "Failed to save department order"}, 500)

    log_action(
        staff_id=staff_id,
        action="reordered_departments",
        target_table="departments",
        target_id=None,
        details=f"Reordered departments: {', '.join(str(i) for i in ordered_ids)}",
    )

    return {"message": "Department order saved successfully"}, None


def delete_department(conn, department_id, staff_id):
    """Dili tugotan ang pag-delete ug department nga naay members pa nga assigned -
    ang department_id column NOT NULL man gud."""
    department = conn.execute("SELECT * FROM departments WHERE id = ?", (department_id,)).fetchone()
    if department is None:
        return None, _no_ctx({"error": "Department not found"}, 404)

    member_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM members WHERE department_id = ?", (department_id,)
    ).fetchone()["cnt"]
    if member_count > 0:
        noun = "member" if member_count == 1 else "members"
        return None, _no_ctx(
            {
                "error": f"Can't delete \"{department['name']}\" - {member_count} {noun} still assigned to it. Reassign them to another department first."
            },
            400,
        )

    try:
        conn.execute("DELETE FROM departments WHERE id = ?", (department_id,))
        conn.commit()
    except sqlite3.IntegrityError:
        # Backstop for a DB-level FK constraint firing too; keeps the message friendly, not raw.
        conn.rollback()
        return None, _no_ctx(
            {"error": "This department can't be deleted because members are still assigned to it"}, 400
        )

    log_action(
        staff_id=staff_id,
        action="deleted_department",
        target_table="departments",
        target_id=department_id,
        target_name=department["name"],
        details=f"Deleted department: {department['name']}",
    )

    return {"message": "Department deleted successfully"}, None


# # # BULK IMPORT (CSV) ------------------------------------------------------


def import_members_csv(content, mode, staff_id):
    """Bulk-add ug members gikan sa CSV; 'preview' mo-rollback, 'commit' mo-keep sa
    changes. Naay kaugalingong connection kay gikinahanglan ang raw isolation_level/
    SAVEPOINT control nga wala gi-offer sa shared conn."""
    dry_run = mode == "preview"

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []
    header_map = {h: (h or "").strip().lower().replace(" ", "_") for h in raw_fieldnames}
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_MEMBER_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return None, _no_ctx(
            {"error": f"CSV missing required column(s): {', '.join(missing_cols)}"}, 400
        )

    conn = get_db_connection()
    # isolation_level=None stops sqlite3 auto-committing each row on SAVEPOINT/ROLLBACK TO.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_departments = [], [], []
    seen_department_names = set()

    try:
        _import_members_rows(reader, header_map, conn, created, errors, new_departments, seen_department_names)
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
        # Nothing was persisted, so no log_action here - same as import_assets_csv.
        return {
            "dry_run": True,
            "rows_to_create": len(created),
            "errors": errors,
            "new_departments": new_departments,
        }, None

    log_action(
        staff_id=staff_id,
        action="bulk_imported_members",
        target_table="members",
        details=f"Imported {len(created)} member(s) via CSV, {len(errors)} error(s)",
    )

    return {
        "dry_run": False,
        "created_count": len(created),
        "created": created,
        "errors": errors,
    }, None


def _import_members_rows(reader, header_map, conn, created, errors, new_departments, seen_department_names):
    """Ang per-row nga body sa import_members_csv(), gi-separate para ang outer
    function's try/finally maka-guarantee nga mo-run gihapon ang conn.close() bisan
    unsa pa mahitabo diri. Mag-auto-create ug bag-ong department kung wala pa nay
    existing name-match."""
    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        first_name = (row.get("first_name") or "").strip()
        last_name = (row.get("last_name") or "").strip()
        # barcode_id isn't read from the CSV - always auto-generated to match member_code, even on import.
        department_name = (row.get("department") or "").strip()
        member_type = (row.get("member_type") or "").strip().lower()

        if not first_name or not last_name or not department_name:
            errors.append({"row": row_num, "error": "first_name, last_name, and department are required"})
            continue

        if member_type not in VALID_MEMBER_TYPES:
            errors.append(
                {"row": row_num, "error": f"member_type must be one of: {', '.join(VALID_MEMBER_TYPES)}"}
            )
            continue

        conn.execute("SAVEPOINT row_import")
        try:
            dept_row = conn.execute(
                "SELECT id FROM departments WHERE name = ?", (department_name,)
            ).fetchone()
            if dept_row is None:
                next_order = conn.execute(
                    "SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM departments"
                ).fetchone()["next_order"]
                department_id = conn.execute(
                    "INSERT INTO departments (name, sort_order) VALUES (?, ?)",
                    (department_name, next_order),
                ).lastrowid
                if department_name not in seen_department_names:
                    seen_department_names.add(department_name)
                    new_departments.append(department_name)
            else:
                department_id = dept_row["id"]

            cursor = conn.execute(
                """
                INSERT INTO members (first_name, last_name, middle_name, suffix, barcode_id, batch, department_id, member_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    first_name,
                    last_name,
                    row.get("middle_name") or None,
                    row.get("suffix") or None,
                    "",
                    row.get("batch") or None,
                    department_id,
                    member_type,
                ),
            )
            new_id = cursor.lastrowid
            generated_code = f"{get_member_code_prefix(conn)}{new_id:05d}"
            conn.execute(
                "UPDATE members SET member_code = ?, barcode_id = ? WHERE id = ?",
                (generated_code, generated_code, new_id),
            )
            conn.execute("RELEASE row_import")
            created.append({"row": row_num, "id": new_id, "barcode_id": generated_code})
        except sqlite3.IntegrityError as e:
            conn.execute("ROLLBACK TO row_import")
            conn.execute("RELEASE row_import")
            errors.append({"row": row_num, "error": f"duplicate or constraint violation: {e}"})


# # # EXPORT --------------------------------------------------------------


def default_members_export_basename(show_inactive):
    """Default filename para sa members export (active/inactive + today's date),
    walay extension pa."""
    return f"members_{'inactive' if show_inactive else 'active'}_{datetime.now().strftime('%Y%m%d')}"


def get_members_for_export(conn, show_inactive):
    """Members (active o inactive, base sa `show_inactive`) nga i-export, sorted by name."""
    where_clause = "members.is_active = 0" if show_inactive else "members.is_active = 1"
    return conn.execute(
        f"""
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE {where_clause}
        ORDER BY members.last_name, members.first_name
        """
    ).fetchall()


def resolve_export_columns(selected):
    """Mag-filter sa MEMBERS_EXPORT_COLUMNS base sa user's `selected` list; mo-fallback
    sa tanan columns kung walay valid nga na-select."""
    if selected:
        columns = [c for c in MEMBERS_EXPORT_COLUMNS if c in selected]
        if not columns:
            columns = list(MEMBERS_EXPORT_COLUMNS)
    else:
        columns = list(MEMBERS_EXPORT_COLUMNS)
    return columns


def build_members_export_file(members, columns, export_format, show_inactive):
    """Mag-build ug export file (xlsx o csv). Returns (file_bytes, content_type,
    export_format) - ang export_format pwede ma-normalize balik sa 'csv' kung naay
    unrecognized format nga naka-sagol."""
    if export_format == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Inactive Members" if show_inactive else "Members"

        header_font = Font(bold=True)
        ws.append([MEMBERS_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for cell in ws[1]:
            cell.font = header_font

        for member in members:
            ws.append([member[col] if member[col] is not None else "" for col in columns])

        for i, col in enumerate(columns, start=1):
            longest = max([len(col)] + [len(str(member[col] or "")) for member in members])
            ws.column_dimensions[get_column_letter(i)].width = min(max(longest + 2, 10), 40)
        ws.freeze_panes = "A2"

        buf = io.BytesIO()
        wb.save(buf)
        return buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx"

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([MEMBERS_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
    for member in members:
        writer.writerow([member[col] if member[col] is not None else "" for col in columns])
    return output.getvalue().encode("utf-8-sig"), "text/csv", "csv"


def save_export_copy(file_bytes, save_dir, filename):
    """Best-effort server-side copy sa export file; returns (saved_path_or_None,
    warning_or_None) - dili ni mag-raise kung mapakyas, warning ra."""
    try:
        os.makedirs(save_dir, exist_ok=True)
        saved_path = os.path.join(save_dir, filename)
        with open(saved_path, "wb") as f:
            f.write(file_bytes)
        return saved_path, None
    except OSError as e:
        logger.warning(f"Could not save export copy to disk: {e}")
        return None, "Could not save a copy on the server; your download still completed."
