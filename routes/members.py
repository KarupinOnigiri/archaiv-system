from flask import Blueprint, jsonify, g, request, render_template, make_response
import json
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
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

    # Truthiness check (not "is not None") because the filter <select>'s
    # unset/placeholder option submits as an empty string, not an absent
    # param -- and hx-include on the sort/page/limit controls sends that
    # empty string on every request, not just ones where this filter was
    # actually touched. Falling through to "is not None" here would silently
    # apply "is_restricted = 0" on every sort/page/limit action, hiding
    # restricted members whenever nothing was actually chosen in the filter.
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


@members_bp.route("/members/stat-cards")
@login_required
def member_stat_cards():
    """Recomputes the four summary counts and re-renders the stat-card
    partial. Fired by the `members-refreshed from:body` listener on
    #member-stat-cards-container in members.html -- dispatched after every
    restrict/unrestrict/deactivate/reactivate action -- so the cards stay in
    sync with whatever just changed without a full page reload. This route
    was simply missing before, which is why every one of those four actions
    was throwing a 404 here and tripping the global htmx:responseError
    toast alongside the action's own success toast.

    Mirrors the exact same queries members_page() (routes/pages.py) uses
    for the initial full-page render, so the two can never drift out of
    sync on what counts as active/restricted/inactive.
    """
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
    """Exact-match barcode lookup for the Borrowers search box's Enter-key
    shortcut (members.html) -- see assets.py:asset_by_barcode for the
    full reasoning, identical here. Matches regardless of active/
    restricted state -- scanning a deactivated member's barcode should
    still show their profile (which already displays that state), not
    act like they don't exist."""
    conn = get_db_connection()
    member = conn.execute(
        "SELECT id FROM members WHERE barcode_id = ?", (barcode,)
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

    # Surfaces an already-pending unrestriction request right on the
    # profile, rather than leaving it discoverable only by going to
    # Approvals and searching -- a tool_keeper looking at a restricted
    # member has no other way to tell "someone already flagged this" from
    # "nobody has yet", and the create endpoint already 409s on a duplicate
    # so this is purely about surfacing what it silently already knows.
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

    # barcode_id is no longer taken from the form -- it's auto-generated
    # below to match the member's own generated member_code, same
    # numbering, no dash (e.g. member_code "MBR00042" -> barcode_id
    # "MBR00042" too). Also what a staff account derived from this member
    # scans/logs in with (see routes/staff.py: get_staff_by_barcode).
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


@members_bp.route("/members/<int:id>/request-unrestriction-form")
@roles_required("tool_keeper")
def request_unrestriction_form(id):
    """Renders the modal a tool_keeper uses to flag a restricted member for
    review, submitting to requests_bp's POST /unrestriction-requests (the
    create endpoint already existed and worked -- this is the missing UI
    that actually calls it; before this, a tool_keeper looking at a
    restricted member had no button anywhere to request it be lifted).
    admin/in_charge don't get this -- they have direct "Lift Restriction"
    instead, same as restrict/unrestrict's existing split."""
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


# # # BULK ACTIONS -------------------------------------------------------


@members_bp.route("/members/bulk-deactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_deactivate_members():
    """Deactivates multiple members in one request. Mirrors
    bulk_delete_assets's shape (deleted/skipped -> deactivated/skipped): a
    single problem row doesn't fail the whole batch, it's recorded in
    `skipped` and the rest proceed, since the caller is a multi-select UI
    where "some of these can't be deactivated right now" is an expected
    outcome, not an exceptional one.

    Every guard from the single-item delete_member route applies per-row
    here too (already deactivated, open borrow transaction, open checkout
    session, linked active staff account), since bulk is just that same
    route run in a loop -- not a shortcut around its rules."""
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
            conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (member_id,))
            if linked_staff is not None:
                conn.execute(
                    "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL WHERE id = ?",
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
    """Reactivates multiple members in one request. Mirrors
    bulk_reactivate_members's sibling bulk_deactivate_members above, and --
    like the single-item reactivate_member route it fans out -- admin/
    in_charge, not admin-only, since reactivating carries none of the
    "reverses a disposal" weight that made asset bulk-restore admin-only."""
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
            conn.execute("UPDATE members SET is_active = 1 WHERE id = ?", (member_id,))
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


# # # BULK IMPORT (CSV) ------------------------------------------------------


REQUIRED_MEMBER_CSV_COLUMNS = ("first_name", "last_name", "department", "member_type")
OPTIONAL_MEMBER_CSV_COLUMNS = ("middle_name", "suffix", "batch")


@members_bp.route("/members/import-form")
@roles_required("admin", "in_charge")
def import_members_form():
    return render_template("members/_import_modal.html", error=None)


def _members_import_error_response(error_msg, status):
    """Mirrors assets.py's _import_error_response: JSON for API callers,
    the modal re-rendered with an inline error for htmx/form callers. See
    that function's docstring for why HX-Retarget/HX-Reswap are needed --
    same reasoning applies here (the preview step targets an inner div,
    not the modal itself)."""
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
    """Bulk-adds members from a CSV, mirroring routes/assets.py's
    import_assets_csv: a dry-run 'preview' mode that runs every row for
    real (including auto-created departments and actual UNIQUE-constraint
    checks) then rolls the whole batch back, and a 'commit' mode that
    keeps it. See import_assets_csv's comments for why manual
    BEGIN/SAVEPOINT/ROLLBACK transaction control is needed here."""
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
    # See import_assets_csv's comment on isolation_level=None -- same
    # reasoning: raw SAVEPOINT/RELEASE/ROLLBACK TO statements would
    # otherwise silently commit the previous row via sqlite3's default
    # implicit-transaction handling.
    conn.isolation_level = None
    conn.execute("BEGIN")
    created, errors, new_departments = [], [], []
    seen_department_names = set()

    for row_num, raw_row in enumerate(reader, start=2):  # row 1 is the header
        row = {header_map[h]: v for h, v in raw_row.items() if h in header_map}
        first_name = (row.get("first_name") or "").strip()
        last_name = (row.get("last_name") or "").strip()
        # barcode_id is no longer read from the CSV -- same reasoning as
        # import_assets_csv: every member gets an auto-generated barcode
        # matching its generated member_code, even on a bulk import.
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
        # Nothing was persisted -- see import_assets_csv's identical note on
        # why this deliberately skips log_action.
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

# Deliberately excludes nothing sensitive -- unlike staff, a member record
# has no password/credential fields, so everything shown in the table is
# fair game to export.
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
    """Read-only, so any logged-in staff can open it -- mirrors
    assets.py's export_assets_form. show_inactive is passed through so
    the modal's default filename/description matches whichever table view
    (active/inactive) the user was looking at."""
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
    """Mirrors routes/assets.py's export_assets: read-only for GET so any
    logged-in staff can use it, respects the active/inactive toggle the
    same way the Export Data button already passes it, supports CSV/Excel
    and a user-chosen column subset and filename. POST additionally saves
    a copy on the server (see export_assets's docstring for why that's
    gated to POST specifically)."""
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
