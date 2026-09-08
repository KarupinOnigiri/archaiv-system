from flask import Blueprint, jsonify, g, request, render_template, make_response
import json
from database import get_db_connection, get_json_or_error
from services.auth_service import roles_required, login_required
from services.settings_service import is_feature_enabled, get_feature_label
from services import member_service
from utils.validators import parse_pagination_params
from utils.exports import sanitize_export_filename, default_export_dir, resolve_export_dir
from extensions import limiter

"""Thin HTTP layer para sa /members ug /departments endpoints - validation
ug business logic naa sa services/member_service.py, dinhi lang ang
request parsing, response shaping (JSON vs htmx partial), ug role gates."""

members_bp = Blueprint("members", __name__)


@members_bp.before_request
def _block_if_members_disabled():
    """Mag-block sa tanang /members* request kung disabled ang Members
    feature toggle - 403 dayon before ma-abot ang actual route."""
    if not is_feature_enabled("members"):
        return jsonify({"error": f"{get_feature_label('members')} is currently disabled"}), 403


# # # LIST / VIEW ------------------------------------------------------------


def get_members_table_context(args):
    """Naay kaugalingong connection; gi-share ni sa view_members() diri ug
    sa members_page() sa routes/pages.py."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    conn = get_db_connection()
    try:
        context, error = member_service.get_members_table_context(conn, args, limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return None, (jsonify(error_payload), status)
    return context, None


@members_bp.route("/members")
@login_required
def view_members():
    """GET - main members list, JSON kung API caller, htmx partial kung
    naay HX-Request header. Delegates sa get_members_table_context() diri."""
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
    """GET - i-recompute ang stat cards, gi-fire pag `members-refreshed`
    after restrict/deactivate actions. Delegates sa get_member_stat_cards()."""
    conn = get_db_connection()
    try:
        stats = member_service.get_member_stat_cards(conn)
    finally:
        conn.close()
    return render_template("members/_stat_cards.html", **stats)


@members_bp.route("/members/by-barcode/<string:barcode>")
@login_required
def member_by_barcode(barcode):
    """GET - exact-match barcode lookup para sa search box's Enter key;
    naay match bisan deactivated na ang member."""
    conn = get_db_connection()
    try:
        member_id = member_service.find_member_id_by_barcode(conn, barcode)
    finally:
        conn.close()
    if member_id is None:
        return render_template(
            "_barcode_not_found_modal.html", kind="borrower", barcode=barcode
        )
    return member_profile(member_id)


@members_bp.route("/members/<int:id>/profile")
@login_required
def member_profile(id):
    """GET - read-only profile modal; ang uban details mag-load client-side
    via separate transaction/history calls."""
    conn = get_db_connection()
    try:
        result, error = member_service.get_member_profile(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "members/_profile_modal.html",
        member=result["member"],
        pending_unrestriction_request=result["pending_unrestriction_request"],
    )


@members_bp.route("/members/add-form")
@roles_required("admin", "in_charge")
def add_member_form():
    """GET - i-render ang blank Add Member modal kauban ang department
    dropdown options."""
    conn = get_db_connection()
    try:
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    return render_template("members/_add_modal.html", departments=departments)


# # # CREATE ------------------------------------------------------------


def _add_member_form_error(error_msg):
    """Re-render ang Add Member modal with inline error message, dili
    mo-close ang modal aron dili mawala ang na-type na sa user."""
    conn = get_db_connection()
    try:
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    return (
        render_template("members/_add_modal.html", departments=departments, error=error_msg),
        200,
    )


@members_bp.route("/members/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_member_submit():
    """POST - mag-create ug bag-ong member. Validates required fields diri
    then delegates sa member_service.create_member(); JSON response o
    HX-Redirect balik sa /members depende sa request type."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    # barcode_id auto-generates to match member_code; a linked staff account also scans this to log in.
    required_fields = ["first_name", "last_name", "department_id", "member_type"]
    for field in required_fields:
        if field not in data or not data[field]:
            error_msg = f"Missing required field: {field}"
            if request.is_json:
                return jsonify({"error": error_msg}), 400
            return _add_member_form_error(error_msg)

    conn = get_db_connection()
    try:
        result, error = member_service.create_member(conn, data, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        error_msg = error_payload.get("error")
        if request.is_json:
            return jsonify(error_payload), status
        return _add_member_form_error(error_msg)

    if request.is_json:
        return (
            jsonify(
                {
                    "message": "Member added successfully",
                    "id": result["id"],
                    "member_code": result["member_code"],
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
    """GET - i-render ang Edit Member modal, pre-filled sa existing member
    data plus department options."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_edit(conn, id)
        if error:
            error_payload, status, _ctx = error
            return jsonify(error_payload), status
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    return render_template("members/_edit_modal.html", member=member, departments=departments)


def _member_edit_error_response(error_payload, status, member):
    """JSON kung JSON request, else i-re-render ang edit modal with inline
    error. Mirrors sa assets.py's pattern."""
    if request.is_json:
        return jsonify(error_payload), status
    conn = get_db_connection()
    try:
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    error_msg = error_payload.get("error") if isinstance(error_payload, dict) else error_payload
    return (
        render_template(
            "members/_edit_modal.html", member=member, departments=departments, error=error_msg
        ),
        200,
    )


@members_bp.route("/members/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_member_submit(id):
    """POST - i-update ang member record. Delegates sa
    member_service.update_member(); on error, re-render ang edit modal
    kung naa'y member context, kay dili tanan error naay member row pa."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error = member_service.update_member(conn, id, data, g.staff["id"], g.staff["member_id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if "member" not in ctx:
            return jsonify(error_payload), status
        return _member_edit_error_response(error_payload, status, ctx["member"])

    if request.is_json:
        return jsonify(result)
    response = make_response("", 200)
    response.headers["HX-Redirect"] = "/members"
    return response


# # # RESTRICTION (manual) ------------------------------------------------


@members_bp.route("/members/<int:id>/restrict-form")
@roles_required("admin", "in_charge", "tool_keeper")
def restrict_member_form(id):
    """GET - i-render ang restrict modal para maghatag ug reason before
    i-restrict ang member."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_restrict(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_restrict_modal.html", member=member)


@members_bp.route("/members/<int:id>/restrict", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def restrict_member(id):
    """POST - bisan kinsang staff pwede mag-flag ug member dayon dayon with
    a reason; low-risk ug dali ra i-trigger, pero naka-log gihapon."""
    if request.is_json:
        data, error = get_json_or_error()
        if error:
            return error
    else:
        data = request.form

    conn = get_db_connection()
    try:
        result, error = member_service.restrict_member(conn, id, data.get("restriction_reason"), g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if request.is_json:
            return jsonify(error_payload), status
        if "member" not in ctx:
            return jsonify(error_payload), status
        return render_template("members/_restrict_modal.html", member=ctx["member"], error=error_payload.get("error")), 200

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{result['name']} restricted successfully",
                "type": "success",
            },
        }
    )
    return response


@members_bp.route("/members/<int:id>/unrestrict-confirm")
@roles_required("admin", "in_charge")
def unrestrict_member_confirm(id):
    """GET - confirm modal before lifting a member's restriction."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_unrestrict(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_unrestrict_modal.html", member=member, error=None)


@members_bp.route("/members/<int:id>/unrestrict", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def unrestrict_member(id):
    """POST - mag-lift sa restriction, mo-balik ang borrowing access, so
    intentional nga naay higher authority requirement kaysa restricting -
    dili pwede tawagon sa tool_keeper."""
    conn = get_db_connection()
    try:
        result, error = member_service.unrestrict_member(conn, id, g.staff["id"], g.staff["member_id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if request.is_json:
            return jsonify(error_payload), status
        if "member" not in ctx:
            return jsonify(error_payload), status
        return render_template("members/_unrestrict_modal.html", member=ctx["member"], error=error_payload.get("error")), 200

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {"message": "Restriction lifted", "type": "success"},
        }
    )
    return response


@members_bp.route("/members/<int:id>/request-unrestriction-form")
@roles_required("tool_keeper")
def request_unrestriction_form(id):
    """GET - tool_keeper's modal para i-flag ang restricted member para
    i-review; admin/in_charge diretso na lang mo-lift."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_unrestriction_request(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_request_unrestriction_modal.html", member=member)


# # # DELETE (deactivate) -----------------------------------------------


def _deactivate_error_response(error_payload, status, member):
    """JSON kung JSON request; i-re-render ang deactivate confirm modal
    with inline error kung htmx/form request."""
    if request.is_json:
        return jsonify(error_payload), status
    error_msg = error_payload.get("error") if isinstance(error_payload, dict) else error_payload
    return render_template("members/_delete_modal.html", member=member, error=error_msg), 200


@members_bp.route("/members/<int:id>/delete-confirm")
@roles_required("admin", "in_charge")
def delete_member_confirm(id):
    """GET - confirm modal before deactivating (soft-delete) a member."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_delete(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_delete_modal.html", member=member, error=None)


@members_bp.route("/members/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_member(id):
    """POST - mag-deactivate (soft-delete) sa member. Delegates sa
    member_service.deactivate_member()."""
    conn = get_db_connection()
    try:
        result, error = member_service.deactivate_member(conn, id, g.staff["id"], g.staff["role"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if "member" not in ctx:
            return jsonify(error_payload), status
        return _deactivate_error_response(error_payload, status, ctx["member"])

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{result['name']} deactivated successfully",
                "type": "success",
            },
        }
    )
    return response


@members_bp.route("/members/<int:id>/reactivate-confirm")
@roles_required("admin", "in_charge")
def reactivate_member_confirm(id):
    """GET - confirm modal before reactivating a deactivated member."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_for_reactivate(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_reactivate_modal.html", member=member, error=None)


@members_bp.route("/members/<int:id>/reactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reactivate_member(id):
    """POST - mag-reactivate sa deactivated member. Delegates sa
    member_service.reactivate_member()."""
    conn = get_db_connection()
    try:
        result, error = member_service.reactivate_member(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, ctx = error
        if request.is_json:
            return jsonify(error_payload), status
        if "member" not in ctx:
            return jsonify(error_payload), status
        return render_template("members/_reactivate_modal.html", member=ctx["member"], error=error_payload.get("error")), 200

    if request.is_json:
        return jsonify({"message": result["message"]})
    response = make_response("", 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    response.headers["HX-Trigger"] = json.dumps(
        {
            "members-refreshed": True,
            "show-toast": {
                "message": f"{result['name']} reactivated successfully",
                "type": "success",
            },
        }
    )
    return response


# # # BULK ACTIONS -------------------------------------------------------


def _parse_bulk_ids(data):
    """I-validate ug i-parse ang `ids` field sa bulk request body into a
    list of ints - error tuple dayon kung wala or sayop ang format."""
    raw_ids = data.get("ids")
    if not raw_ids or not isinstance(raw_ids, list):
        return None, (
            jsonify({"error": "Missing required field: ids (must be a non-empty list)"}),
            400,
        )
    try:
        return [int(i) for i in raw_ids], None
    except (ValueError, TypeError):
        return None, (jsonify({"error": "ids must be a list of integers"}), 400)


@members_bp.route("/members/bulk-deactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_deactivate_members():
    """POST - bulk deactivate; kung naay bad row, mo-adto ra na sa
    `skipped` list, same per-row guards sa single delete_member()."""
    data, error = get_json_or_error()
    if error:
        return error

    ids, error = _parse_bulk_ids(data)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, _error = member_service.bulk_deactivate_members(conn, ids, g.staff["id"], g.staff["role"])
    finally:
        conn.close()

    return jsonify(result)


@members_bp.route("/members/bulk-reactivate", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("10 per minute")
def bulk_reactivate_members():
    """POST - bulk reactivate, open sa admin/in_charge; lighter weight
    kaysa sa asset restore's admin-only gate."""
    data, error = get_json_or_error()
    if error:
        return error

    ids, error = _parse_bulk_ids(data)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, _error = member_service.bulk_reactivate_members(conn, ids, g.staff["id"])
    finally:
        conn.close()

    return jsonify(result)


# # # API ROUTES (scanning lookups) ------------------------------------------


@members_bp.route("/members/barcode/<string:barcode>")
@login_required
@limiter.limit("40 per minute")
def get_member_by_barcode(barcode):
    """GET - API lookup para sa scanning flows (e.g. checkout screen);
    JSON-only, lahi ni sa member_by_barcode() nga UI/modal version."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_member_by_barcode(conn, barcode)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(member)


@members_bp.route("/members/<int:id>/transactions")
@login_required
def get_member_transactions(id):
    """GET - paginated JSON list sa member's transactions, optionally
    filtered by `status` query param."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = member_service.get_member_transactions(
            conn, id, limit, offset, request.args.get("status")
        )
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@members_bp.route("/members/<int:id>/borrow-history-modal")
@login_required
def borrow_history_modal(id):
    """GET - "View Full History" shell para sa Borrow History; ang rows
    mag-load gikan sa partial below via htmx."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_borrow_history_modal_member(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_borrow_history_modal.html", member=member)


@members_bp.route("/members/<int:id>/borrow-history-modal/table")
@login_required
def borrow_history_modal_table(id):
    """GET - htmx partial nga nag-back sa borrow_history_modal - same
    query sa get_member_transactions pero actually paginated instead of
    fixed limit=3."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = member_service.get_borrow_history_table(conn, id, limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "members/_borrow_history_table.html",
        member_id=id,
        transactions=result["transactions"],
        total_count=result["total_count"],
        limit=result["limit"],
        offset=result["offset"],
    )


@members_bp.route("/members/<int:id>/restriction-history")
@login_required
def get_member_restriction_history(id):
    """GET - JSON teaser para sa Restriction History tab; open sa bisan
    kinsang staff, including tool_keeper."""
    conn = get_db_connection()
    try:
        result, error = member_service.get_restriction_history(conn, id)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@members_bp.route("/members/<int:id>/restriction-history-modal")
@login_required
def restriction_history_modal(id):
    """GET - "View Full History" shell para sa Restriction History; ang
    rows mag-load gikan sa partial below."""
    conn = get_db_connection()
    try:
        member, error = member_service.get_restriction_history_modal_member(conn, id)
    finally:
        conn.close()
    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return render_template("members/_restriction_history_modal.html", member=member)


@members_bp.route("/members/<int:id>/restriction-history-modal/table")
@login_required
def restriction_history_modal_table(id):
    """GET - paginated htmx partial; ang audit_log ug
    unrestriction_requests kay UNION ALL'd into one timeline."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = member_service.get_restriction_history_table(conn, id, limit, offset)
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return render_template(
        "members/_restriction_history_table.html",
        member_id=id,
        rows=result["rows"],
        total_count=result["total_count"],
        limit=result["limit"],
        offset=result["offset"],
    )


# # # DEPARTMENTS (lookup table) -----------------------------------------


@members_bp.route("/departments/manage-form")
@roles_required("admin", "in_charge")
def manage_departments_form():
    """GET - departments management modal, kauban ang member_count per
    department para makita kung pwede ba i-delete."""
    conn = get_db_connection()
    try:
        departments = member_service.get_departments_with_member_count(conn)
    finally:
        conn.close()
    return render_template("members/_departments_modal.html", departments=departments)


@members_bp.route("/departments")
@login_required
def view_departments():
    """GET - JSON list sa tanang departments."""
    conn = get_db_connection()
    try:
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    return jsonify([dict(row) for row in departments])


@members_bp.route("/departments/options")
@login_required
def department_options():
    """GET - <option> tags para sa department dropdown, refetched pag naay
    `departments-changed` event para dili mo-stale."""
    conn = get_db_connection()
    try:
        departments = member_service.list_departments(conn)
    finally:
        conn.close()
    return render_template("members/_department_options.html", departments=departments)


@members_bp.route("/departments/add", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def add_department():
    """POST - mag-create ug bag-ong department. Delegates sa
    member_service.add_department()."""
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = member_service.add_department(conn, data.get("name"), g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status

    return jsonify({"message": "Department added successfully", **result}), 201


@members_bp.route("/departments/<int:id>/edit", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def edit_department(id):
    """POST - i-rename ang department. Delegates sa
    member_service.edit_department()."""
    data, error = get_json_or_error()
    if error:
        return error

    conn = get_db_connection()
    try:
        result, error = member_service.edit_department(conn, id, data.get("name"), g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@members_bp.route("/departments/reorder", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def reorder_departments():
    """POST - i-save ang bag-ong department order; kinahanglan full id set
    aron dili ma-orphan ang usa ka department sa partial request."""
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

    conn = get_db_connection()
    try:
        result, error = member_service.reorder_departments(conn, ordered_ids, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


@members_bp.route("/departments/<int:id>/delete", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def delete_department(id):
    """POST - i-block ang pag-delete sa department kung naay members pa
    nga assigned; department_id is NOT NULL man sa DB."""
    conn = get_db_connection()
    try:
        result, error = member_service.delete_department(conn, id, g.staff["id"])
    finally:
        conn.close()

    if error:
        error_payload, status, _ctx = error
        return jsonify(error_payload), status
    return jsonify(result)


# # # BULK IMPORT (CSV) ------------------------------------------------------


@members_bp.route("/members/import-form")
@roles_required("admin", "in_charge")
def import_members_form():
    """GET - i-render ang blank CSV Import modal."""
    return render_template("members/_import_modal.html", error=None)


def _members_import_error_response(error_msg, status):
    """Mirrors sa assets.py's _import_error_response: JSON para sa API
    callers, modal re-render otherwise."""
    if request.is_json:
        return jsonify({"error": error_msg}), status
    response = make_response(render_template("members/_import_modal.html", error=error_msg), 200)
    response.headers["HX-Retarget"] = "#modal-container"
    response.headers["HX-Reswap"] = "innerHTML"
    return response


@members_bp.route("/members/import", methods=["POST"])
@roles_required("admin", "in_charge")
@limiter.limit("5 per minute")
def import_members_csv():
    """POST - bulk-add ug members gikan sa CSV, mirroring
    import_assets_csv; 'preview' mode mo-rollback, 'commit' mo-keep."""
    if "file" not in request.files:
        return _members_import_error_response("Missing file upload (field name: 'file')", 400)

    file = request.files["file"]
    if not file.filename:
        return _members_import_error_response("No file selected", 400)

    mode = request.form.get("mode", "commit")
    if mode not in ("preview", "commit"):
        return _members_import_error_response("mode must be 'preview' or 'commit'", 400)

    try:
        content = file.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError:
        return _members_import_error_response("File must be UTF-8 encoded CSV", 400)

    result, error = member_service.import_members_csv(content, mode, g.staff["id"])

    if error:
        error_payload, status, _ctx = error
        return _members_import_error_response(error_payload.get("error"), status)

    if result["dry_run"]:
        return render_template(
            "members/_import_preview.html",
            rows_to_create=result["rows_to_create"],
            errors=result["errors"],
            new_departments=result["new_departments"],
        )

    if request.is_json:
        return (
            jsonify(
                {
                    "created_count": result["created_count"],
                    "created": result["created"],
                    "errors": result["errors"],
                }
            ),
            201 if result["created"] else 400,
        )

    response = make_response(
        render_template(
            "members/_import_results.html",
            created_count=result["created_count"],
            created=result["created"],
            errors=result["errors"],
        )
    )
    if result["created"]:
        response.headers["HX-Trigger"] = "members-refreshed"
    return response


# # # EXPORT --------------------------------------------------------------


@members_bp.route("/members/export-form")
@login_required
def export_members_form():
    """GET - read-only, mirrors assets.py's export_assets_form. Gi-pass
    ang show_inactive para ang modal's default filename mo-match sa table
    view nga gigikanan sa user."""
    show_inactive = request.args.get("show_inactive") == "1"
    return render_template(
        "members/_export_modal.html",
        show_inactive=show_inactive,
        default_filename=member_service.default_members_export_basename(show_inactive),
        default_export_dir=default_export_dir(),
        fields=[
            (col, member_service.MEMBERS_EXPORT_FIELD_LABELS[col])
            for col in member_service.MEMBERS_EXPORT_COLUMNS
        ],
    )


@members_bp.route("/members/export", methods=["GET", "POST"])
@login_required
def export_members():
    """Mirrors assets.py's export_assets: GET kay read-only CSV/Excel
    download, POST kay mag-save pud ug copy server-side."""
    show_inactive = request.values.get("show_inactive") == "1"
    export_format = request.values.get("format", "csv").lower()
    if export_format not in ("csv", "xlsx"):
        export_format = "csv"

    columns = member_service.resolve_export_columns(request.values.getlist("fields"))

    conn = get_db_connection()
    try:
        members = member_service.get_members_for_export(conn, show_inactive)
    finally:
        conn.close()

    basename = sanitize_export_filename(
        request.values.get("filename"), member_service.default_members_export_basename(show_inactive)
    )

    file_bytes, content_type, export_format = member_service.build_members_export_file(
        members, columns, export_format, show_inactive
    )
    filename = f"{basename}.{export_format}"

    response = make_response(file_bytes)
    response.headers["Content-Type"] = content_type
    response.headers["Content-Disposition"] = f"attachment; filename={filename}"

    if request.method == "POST":
        save_dir, was_clamped = resolve_export_dir(request.values.get("save_path"))
        saved_path, warning = member_service.save_export_copy(file_bytes, save_dir, filename)
        if saved_path:
            response.headers["X-Export-Saved-Path"] = saved_path
            if was_clamped:
                response.headers["X-Export-Warning"] = (
                    "The folder entered was outside the app directory, "
                    "so the export was saved to the default export folder instead."
                )
        elif warning:
            response.headers["X-Export-Warning"] = warning

    return response
