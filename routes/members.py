from flask import Blueprint, jsonify, g, request, render_template, make_response
import json
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.settings_service import is_feature_enabled, get_feature_label
from utils.logger import log_action
from utils.formatters import full_name
from utils.validators import parse_pagination_params, validate_lookup_id
from utils.exports import sanitize_export_filename, default_export_dir, resolve_export_dir
import sqlite3
import csv
import io
import os
from datetime import datetime
from extensions import limiter
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import logging

logger = logging.getLogger(__name__)

members_bp = Blueprint("members", __name__)


@members_bp.before_request
def _block_if_members_disabled():
    if not is_feature_enabled("members"):
        return jsonify({"error": f"{get_feature_label('members')} is currently disabled"}), 403
VALID_MEMBER_TYPES = ("trainee", "trainer")

# Flat cap instead of real pagination -- see get_member_restriction_history docstring.
RESTRICTION_HISTORY_CAP = 20


# # # LIST / VIEW ------------------------------------------------------------

# "name" maps to last_name + first_name (no single full-name column), both sorted the same direction.
SORTABLE_COLUMNS = {
    "name": "members.last_name, members.first_name",
    "member_code": "members.member_code",
    "department": "departments.name",
    "member_type": "members.member_type",
    "barcode_id": "members.barcode_id",
    "batch": "members.batch",
}


def get_members_table_context(args):
    """Shared query logic for the members table, used by both the htmx partial route
    and the full-page /members route so they can't drift apart on filters/columns.
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

    # Truthiness check on purpose: the unset filter <select> submits "" (not absent),
    # and hx-include resends that on every sort/page request. "is not None" would treat
    # that "" as is_restricted=0 and silently hide restricted members.
    is_restricted = args.get("is_restricted")
    if is_restricted:
        if is_restricted.lower() not in ("0", "1", "true", "false"):
            return None, (
                jsonify({"error": "is_restricted must be '0' or '1'"}),
                400,
            )
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

    # sort_column can be one column or several comma-separated ones (name -> last_name, first_name);
    # apply the direction to all of them.
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


@members_bp.route("/members/stat-cards")
@login_required
def member_stat_cards():
    """Recomputes the four summary counts and re-renders the stat-card partial.
    Fired by the `members-refreshed from:body` listener after restrict/unrestrict/
    deactivate/reactivate so the cards update without a full reload.
    Mirrors the same queries as members_page() in routes/pages.py so the two stay in sync."""
    conn = get_db_connection()
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
    conn.close()

    return render_template(
        "members/_stat_cards.html",
        stat_all=stat_all,
        stat_active=stat_active,
        stat_restricted=stat_restricted,
        stat_inactive=stat_inactive,
    )


@members_bp.route("/members/by-barcode/<string:barcode>")
@login_required
def member_by_barcode(barcode):
    """Exact-match barcode lookup for the Borrowers search box's Enter key (see
    assets.py:asset_by_barcode for the same pattern). Matches regardless of active/
    restricted state so scanning a deactivated member still shows their profile."""
    conn = get_db_connection()
    member = conn.execute(
        "SELECT id FROM members WHERE UPPER(barcode_id) = UPPER(?)", (barcode,)
    ).fetchone()
    conn.close()
    if member is None:
        return render_template(
            "_barcode_not_found_modal.html", kind="borrower", barcode=barcode
        )
    return member_profile(member["id"])


@members_bp.route("/members/<int:id>/profile")
@login_required
def member_profile(id):
    """Renders the read-only profile modal. The modal's JS fills in the rest by
    calling /members/<id>/transactions and /members/<id>/restriction-history directly,
    so this route doesn't need to assemble everything server-side."""
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

    # Surface an already-pending unrestriction request on the profile itself, so staff
    # don't have to dig through Approvals to see "someone already flagged this".
    pending_unrestriction_request = None
    if member is not None and member["is_restricted"]:
        pending_unrestriction_request = conn.execute(
            """
            SELECT unrestriction_requests.id, unrestriction_requests.reason,
                   unrestriction_requests.created_at, staff.username AS requested_by_username
            FROM unrestriction_requests
            JOIN staff ON unrestriction_requests.requested_by_staff_id = staff.id
            WHERE unrestriction_requests.member_id = ? AND unrestriction_requests.status = 'pending'
            """,
            (id,),
        ).fetchone()
    conn.close()

    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return render_template(
        "members/_profile_modal.html",
        member=member,
        pending_unrestriction_request=pending_unrestriction_request,
    )


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

    # barcode_id isn't taken from the form -- auto-generated below to match member_code
    # (e.g. "MBR00042" for both). Also what a linked staff account logs in by scanning.
    required_fields = [
        "first_name",
        "last_name",
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
        generated_code = f"MBR{new_member_id:05d}"
        conn.execute(
            "UPDATE members SET member_code = ?, barcode_id = ? WHERE id = ?",
            (generated_code, generated_code, new_member_id),
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
        target_name=full_name(data['first_name'], data['last_name'], data.get('middle_name'), data.get('suffix')),
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
            SET first_name = ?, last_name = ?, middle_name = ?, suffix = ?, department_id = ?, batch = ?, phone_number = ?, is_restricted = ?, restriction_reason = ?, restriction_source = ?
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
        target_name=updated_name,
        details=f"Updated member {updated_name}",
    )

    if old_is_restricted != new_is_restricted:
        if new_is_restricted:
            log_action(
                staff_id=g.staff["id"],
                action="restricted_member",
                target_table="members",
                target_id=member["id"],
                target_name=updated_name,
                details=f"Restricted member {updated_name}: {data.get('restriction_reason', 'No reason given')}",
            )
        else:
            log_action(
                staff_id=g.staff["id"],
                action="unrestricted_member",
                target_table="members",
                target_id=member["id"],
                target_name=updated_name,
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
        target_name=updated_name,
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
        target_name=updated_name,
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


@members_bp.route("/members/<int:id>/request-unrestriction-form")
@roles_required("tool_keeper")
def request_unrestriction_form(id):
    """Modal for a tool_keeper to flag a restricted member for review, posting to
    requests_bp's /unrestriction-requests. admin/in_charge skip this and use
    "Lift Restriction" directly instead."""
    conn = get_db_connection()
    member = conn.execute("SELECT * FROM members WHERE id = ?", (id,)).fetchone()
    conn.close()

    if member is None:
        return jsonify({"error": "Member not found"}), 404

    if not member["is_restricted"]:
        return jsonify({"error": "Member is not currently restricted"}), 400

    return render_template("members/_request_unrestriction_modal.html", member=member)


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
        conn.execute(
            "UPDATE members SET is_active = 0, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
            (id,),
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
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
        details=f"Deactivated member {full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix'])}",
    )

    if linked_staff is not None:
        log_action(
            staff_id=g.staff["id"],
            action="deactivated_staff_cascade",
            target_table="staff",
            target_id=linked_staff["id"],
            target_name=linked_staff["username"],
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
        conn.execute("UPDATE members SET is_active = 1, deactivated_at = NULL WHERE id = ?", (id,))
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
        target_name=full_name(member['first_name'], member['last_name'], member['middle_name'], member['suffix']),
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


# # # BULK ACTIONS -------------------------------------------------------


@members_bp.route("/members/bulk-deactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_deactivate_members():
    """Deactivates multiple members in one request. Mirrors bulk_delete_assets:
    a bad row goes into `skipped` and the rest still proceed. Runs the same
    per-row guards as the single delete_member route (already deactivated, open
    loan/session, linked staff account) -- bulk doesn't skip any of those checks."""
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
    staff_cascades = []  # (staff_id, username) pairs to log after conn.close()

    for member_id in ids:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

        if member is None:
            skipped.append({"id": member_id, "reason": "Member not found"})
            continue

        name = full_name(
            member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
        )

        if not member["is_active"]:
            skipped.append({"id": member_id, "name": name, "reason": "Already deactivated"})
            continue

        open_loan = conn.execute(
            "SELECT id FROM transactions WHERE member_id = ? AND status = 'borrowed' LIMIT 1",
            (member_id,),
        ).fetchone()

        if open_loan is not None:
            skipped.append(
                {
                    "id": member_id,
                    "name": name,
                    "reason": "Currently has one or more tools checked out",
                }
            )
            continue

        open_session = conn.execute(
            "SELECT id FROM checkout_sessions WHERE member_id = ? AND status = 'open'",
            (member_id,),
        ).fetchone()

        if open_session is not None:
            skipped.append(
                {"id": member_id, "name": name, "reason": "Has an open checkout session"}
            )
            continue

        linked_staff = conn.execute(
            "SELECT * FROM staff WHERE member_id = ? AND is_active = 1", (member_id,)
        ).fetchone()

        if linked_staff is not None and g.staff["role"] != "admin":
            skipped.append(
                {
                    "id": member_id,
                    "name": name,
                    "reason": "Has an active staff account -- only an admin can deactivate",
                }
            )
            continue

        if linked_staff is not None and linked_staff["role"] == "admin":
            skipped.append(
                {
                    "id": member_id,
                    "name": name,
                    "reason": "Has an active admin account -- deactivate via staff management instead",
                }
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
            skipped.append(
                {
                    "id": member_id,
                    "name": name,
                    "reason": "A database constraint was violated",
                }
            )

    conn.close()

    if deactivated:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_deactivated_members",
            target_table="members",
            details=(
                f"Bulk deactivated {len(deactivated)} member(s): "
                + ", ".join(m["name"] for m in deactivated)
                + (f"; skipped {len(skipped)}" if skipped else "")
            ),
        )

    for staff_id, username in staff_cascades:
        log_action(
            staff_id=g.staff["id"],
            action="deactivated_staff_cascade",
            target_table="staff",
            target_id=staff_id,
            details=f"Staff account {username} auto-deactivated because linked member was bulk-deactivated",
        )

    return jsonify(
        {
            "deactivated_count": len(deactivated),
            "deactivated": deactivated,
            "skipped": skipped,
        }
    )


@members_bp.route("/members/bulk-reactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_reactivate_members():
    """Reactivates multiple members in one request, mirroring bulk_deactivate_members
    above. Open to admin/in_charge (not admin-only) since reactivating a member
    doesn't carry the "reverses a disposal" weight that asset bulk-restore does."""
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

    for member_id in ids:
        member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

        if member is None:
            skipped.append({"id": member_id, "reason": "Member not found"})
            continue

        name = full_name(
            member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
        )

        if member["is_active"]:
            skipped.append({"id": member_id, "name": name, "reason": "Already active"})
            continue

        try:
            conn.execute(
                "UPDATE members SET is_active = 1, deactivated_at = NULL WHERE id = ?", (member_id,)
            )
            conn.commit()
            reactivated.append({"id": member_id, "name": name})
        except sqlite3.IntegrityError as e:
            conn.rollback()
            logger.error(f"IntegrityError during bulk reactivate of member {member_id}: {e}")
            skipped.append(
                {
                    "id": member_id,
                    "name": name,
                    "reason": "A database constraint was violated",
                }
            )

    conn.close()

    if reactivated:
        log_action(
            staff_id=g.staff["id"],
            action="bulk_reactivated_members",
            target_table="members",
            details=(
                f"Bulk reactivated {len(reactivated)} member(s): "
                + ", ".join(m["name"] for m in reactivated)
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
        WHERE UPPER(members.barcode_id) = UPPER(?)
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


@members_bp.route("/members/<int:id>/borrow-history-modal")
@login_required
def borrow_history_modal(id):
    """"View Full History" destination for the profile modal's Borrow History tab.
    Modal shell only -- rows load from borrow_history_modal_table below via
    hx-trigger="load", same shell+partial split as restriction_history_modal."""
    conn = get_db_connection()
    member = conn.execute(
        """
        SELECT members.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM members WHERE members.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return render_template("members/_borrow_history_modal.html", member=member)


@members_bp.route("/members/<int:id>/borrow-history-modal/table")
@login_required
def borrow_history_modal_table(id):
    """htmx partial backing borrow_history_modal -- same query as get_member_transactions
    but actually paginated instead of a fixed limit=3."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    member = conn.execute("SELECT id FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM transactions WHERE member_id = ?", (id,)
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
        (id, limit, offset),
    ).fetchall()
    conn.close()

    return render_template(
        "members/_borrow_history_table.html",
        member_id=id,
        transactions=transactions,
        total_count=total_count,
        limit=limit,
        offset=offset,
    )


def _fetch_restriction_history(id, conn):
    """Shared query for get_member_restriction_history and restriction_history_modal --
    combines restrict/unrestrict audit events with the unrestriction_requests trail.
    Capped (not paginated) since there's no "view all" page to link to like borrow
    history has; RESTRICTION_HISTORY_CAP is just headroom against unbounded scroll.
    Returns (events, requests), newest first."""
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
        (id, RESTRICTION_HISTORY_CAP),
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
        (id, RESTRICTION_HISTORY_CAP),
    ).fetchall()

    return restriction_events, unrestriction_requests


@members_bp.route("/members/<int:id>/restriction-history")
@login_required
def get_member_restriction_history(id):
    """JSON feed for the profile modal's Restriction History tab teaser.
    Open to any logged-in staff (including tool_keeper) since they need this
    context to judge if a member's ready to borrow again before filing a request."""
    conn = get_db_connection()

    member = conn.execute("SELECT id FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

    restriction_events, unrestriction_requests = _fetch_restriction_history(id, conn)
    conn.close()

    return jsonify(
        {
            "restriction_events": [dict(row) for row in restriction_events],
            "unrestriction_requests": [dict(row) for row in unrestriction_requests],
            "capped_at": RESTRICTION_HISTORY_CAP,
        }
    )


@members_bp.route("/members/<int:id>/restriction-history-modal")
@login_required
def restriction_history_modal(id):
    """"View Full History" destination for Restriction History -- modal shell only,
    rows load from restriction_history_modal_table below (hx-trigger="load"),
    same shell+partial split used by the other full-history popups."""
    conn = get_db_connection()

    member = conn.execute(
        """
        SELECT members.id, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM members WHERE members.id = ?
        """,
        (id,),
    ).fetchone()
    conn.close()
    if member is None:
        return jsonify({"error": "Member not found"}), 404

    return render_template("members/_restriction_history_modal.html", member=member)


@members_bp.route("/members/<int:id>/restriction-history-modal/table")
@login_required
def restriction_history_modal_table(id):
    """htmx partial backing restriction_history_modal, actually paginated (unlike
    the capped teaser). audit_log events and unrestriction_requests are different
    tables with no shared key, so they're normalized and UNION ALL'd into one
    timeline before paging, instead of two separately-capped lists."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    member = conn.execute("SELECT id FROM members WHERE id = ?", (id,)).fetchone()
    if member is None:
        conn.close()
        return jsonify({"error": "Member not found"}), 404

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
    params = (id, id)

    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM ({union_sql})", params
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"{union_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    ).fetchall()
    conn.close()

    return render_template(
        "members/_restriction_history_table.html",
        member_id=id,
        rows=rows,
        total_count=total_count,
        limit=limit,
        offset=offset,
    )

    # # # DEPARTMENTS (lookup table) -----------------------------------------


@members_bp.route("/departments/manage-form")
@roles_required("admin", "in_charge")
def manage_departments_form():
    conn = get_db_connection()
    # LEFT JOIN + COUNT gives member_count = 0 for empty departments instead of NULL/absent,
    # which the delete modal needs to warn "N members will need to be reassigned".
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
    """<option> tags for the department filter dropdown, refetched whenever the Manage
    Departments modal fires `departments-changed`, so the dropdown (which stays
    mounted, unlike the add/edit forms) doesn't go stale without a full reload."""
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
        # New departments go at the end of the order; COALESCE handles the first-ever department (MAX is NULL).
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
        target_name=name,
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
        target_name=name,
        details=f"Renamed department '{department['name']}' to '{name}'",
    )

    return jsonify({"message": "Department renamed successfully"})


@members_bp.route("/departments/reorder", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reorder_departments():
    """Persists department order from the Manage Departments modal.
    Expects {"order": [id, id, ...]} with every department id included --
    requiring the full set stops a stale/partial request from silently
    orphaning a department at its old sort_order."""
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
    """Deletes a department only if no member belongs to it. department_id is
    NOT NULL on members, so unlike the frontend's confirm dialog (which just warns),
    the server hard-refuses and requires reassignment first."""
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
        # Backstop in case a DB-level FK constraint also fires -- keep the message friendly, not a raw 500.
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
        target_name=department["name"],
        details=f"Deleted department: {department['name']}",
    )

    return jsonify({"message": "Department deleted successfully"})


# # # BULK IMPORT (CSV) ------------------------------------------------------


REQUIRED_MEMBER_CSV_COLUMNS = ("first_name", "last_name", "department", "member_type")
OPTIONAL_MEMBER_CSV_COLUMNS = ("middle_name", "suffix", "batch")


@members_bp.route("/members/import-form")
@roles_required("admin", "in_charge")
def import_members_form():
    return render_template("members/_import_modal.html", error=None)


def _members_import_error_response(error_msg, status):
    """Mirrors assets.py's _import_error_response: JSON for API callers, re-rendered
    modal with inline error for htmx/form callers. HX-Retarget/HX-Reswap needed since
    the preview step targets an inner div, not the whole modal."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(
        render_template("members/_import_modal.html", error=error_msg), 200
    )
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@members_bp.route("/members/import", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("5 per minute")
def import_members_csv():
    """Bulk-adds members from a CSV, mirroring routes/assets.py's import_assets_csv:
    'preview' mode runs every row for real (departments, UNIQUE checks) then rolls
    back the whole batch; 'commit' keeps it. See import_assets_csv for why manual
    BEGIN/SAVEPOINT/ROLLBACK is needed."""
    if "file" not in request.files:
        return _members_import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _members_import_error_response("No file selected", 400)

    mode = request.form.get("mode", "commit")
    if mode not in ("preview", "commit"):
        return _members_import_error_response("mode must be 'preview' or 'commit'", 400)
    dry_run = mode == "preview"

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _members_import_error_response("File must be UTF-8 encoded CSV", 400)

    reader = csv.DictReader(io.StringIO(content))
    raw_fieldnames = reader.fieldnames or []
    header_map = {h: (h or "").strip().lower().replace(" ", "_") for h in raw_fieldnames}
    canonical_fieldnames = list(header_map.values())

    missing_cols = [c for c in REQUIRED_MEMBER_CSV_COLUMNS if c not in canonical_fieldnames]
    if missing_cols:
        return _members_import_error_response(
            f"CSV missing required column(s): {', '.join(missing_cols)}", 400
        )

    conn = get_db_connection()
    # isolation_level=None -- otherwise sqlite3's implicit-transaction handling would
    # silently commit the previous row on each SAVEPOINT/RELEASE/ROLLBACK TO. See import_assets_csv.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_departments = [], [], []
    seen_department_names = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        first_name = (row.get("first_name") or "").strip()
        last_name = (row.get("last_name") or "").strip()
        # barcode_id isn't read from the CSV -- always auto-generated to match member_code, even on import.
        department_name = (row.get("department") or "").strip()
        member_type = (row.get("member_type") or "").strip().lower()

        if not first_name or not last_name or not department_name:
            errors.append(
                {
                    "row": row_num,
                    "error": "first_name, last_name, and department are required",
                }
            )
            continue

        if member_type not in VALID_MEMBER_TYPES:
            errors.append(
                {
                    "row": row_num,
                    "error": f"member_type must be one of: {', '.join(VALID_MEMBER_TYPES)}",
                }
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
            generated_code = f"MBR{new_id:05d}"
            conn.execute(
                "UPDATE members SET member_code = ?, barcode_id = ? WHERE id = ?",
                (generated_code, generated_code, new_id),
            )
            conn.execute("RELEASE row_import")
            created.append({"row": row_num, "id": new_id, "barcode_id": generated_code})
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
        # Nothing was persisted, so no log_action here -- same as import_assets_csv.
        return render_template(
            "members/_import_preview.html",
            rows_to_create=len(created),
            errors=errors,
            new_departments=new_departments,
        )

    log_action(
        staff_id=g.staff["id"],
        action="bulk_imported_members",
        target_table="members",
        details=f"Imported {len(created)} member(s) via CSV, {len(errors)} error(s)",
    )

    if request.is_json:
        return jsonify(
            {"created_count": len(created), "created": created, "errors": errors}
        ), (201 if created else 400)

    response = make_response(
        render_template(
            "members/_import_results.html",
            created_count=len(created),
            created=created,
            errors=errors,
        )
    )
    if created:
        response.headers["HX-Trigger"] = "members-refreshed"
    return response


# # # EXPORT --------------------------------------------------------------

# No exclusions needed -- unlike staff, members have no password/credential fields.
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


def _default_members_export_basename(show_inactive):
    return (
        f"members_{'inactive' if show_inactive else 'active'}_"
        f"{datetime.now().strftime('%Y%m%d')}"
    )


@members_bp.route("/members/export-form")
@login_required
def export_members_form():
    """Read-only, mirrors assets.py's export_assets_form. show_inactive is passed
    through so the modal's default filename matches the table view the user was on."""
    show_inactive = request.args.get("show_inactive") == "1"
    return render_template(
        "members/_export_modal.html",
        show_inactive=show_inactive,
        default_filename=_default_members_export_basename(show_inactive),
        default_export_dir=default_export_dir(),
        fields=[(col, MEMBERS_EXPORT_FIELD_LABELS[col]) for col in MEMBERS_EXPORT_COLUMNS],
    )


@members_bp.route("/members/export", methods=["GET", "POST"])
@login_required
def export_members():
    """Mirrors routes/assets.py's export_assets: GET is read-only for any staff,
    supports CSV/Excel with a chosen column subset and filename. POST additionally
    saves a copy server-side (see export_assets for why that's POST-only)."""
    show_inactive = request.values.get("show_inactive") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    selected = request.values.getlist("fields")
    if selected:
        columns = [c for c in MEMBERS_EXPORT_COLUMNS if c in selected]
        if not columns:
            columns = list(MEMBERS_EXPORT_COLUMNS)
    else:
        columns = list(MEMBERS_EXPORT_COLUMNS)

    conn = get_db_connection()
    where_clause = "members.is_active = 0" if show_inactive else "members.is_active = 1"
    members = conn.execute(
        f"""
        SELECT members.*, departments.name AS department
        FROM members
        JOIN departments ON members.department_id = departments.id
        WHERE {where_clause}
        ORDER BY members.last_name, members.first_name
        """
    ).fetchall()
    conn.close()

    basename = sanitize_export_filename(
        request.values.get("filename"), _default_members_export_basename(show_inactive)
    )

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
            longest = max(
                [len(col)] + [len(str(member[col] or "")) for member in members]
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
        writer.writerow([MEMBERS_EXPORT_FIELD_LABELS.get(col, col) for col in columns])
        for member in members:
            writer.writerow([member[col] if member[col] is not None else "" for col in columns])
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
