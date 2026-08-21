from flask import Blueprint, jsonify, request, make_response, g
from database import get_db_connection
from services.auth_service import roles_required
from services.settings_service import is_feature_enabled, get_feature_label
from services.report_widgets import WIDGETS, widgets_for_role
from services.report_layout_service import get_report_layout, save_report_layout, reset_report_layout
from services import stats_service
from utils.validators import validate_date_string, parse_pagination_params
from extensions import limiter

stats_bp = Blueprint("stats", __name__)


@stats_bp.before_request
def _block_if_reports_disabled():
    if not is_feature_enabled("reports"):
        return jsonify({"error": f"{get_feature_label('reports')} is currently disabled"}), 403


# # # ASSET USAGE ---------------------------------------------------------


@stats_bp.route("/stats/assets/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_assets():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.most_borrowed_assets(conn, limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/assets/least-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def least_borrowed_assets():
    """Inverse of most-borrowed, including zero-transaction assets via LEFT JOIN."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.least_borrowed_assets(conn, limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/categories/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_categories():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.most_borrowed_categories(conn, limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/loan-duration")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def loan_duration():
    """Average hours between borrow and return, returned transactions only. group_by=asset or category."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    group_by = request.args.get("group_by", "asset")
    if group_by not in stats_service.VALID_GROUP_BY:
        return (
            jsonify({"error": f"group_by must be one of: {', '.join(stats_service.VALID_GROUP_BY)}"}),
            400,
        )

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.loan_duration(conn, limit, offset, group_by, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


# # # MEMBER BEHAVIOR ------------------------------------------------------


@stats_bp.route("/stats/members/top-borrowers")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def top_borrowers():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.top_borrowers(conn, limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/members/overdue-frequency")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def overdue_frequency():
    """Repeat-lateness history, distinct from members.is_restricted. Splits overdue_count and still_open_count."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.overdue_frequency(conn, limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


# # # CONDITION / DAMAGE ---------------------------------------------------


@stats_bp.route("/stats/assets/condition-changes")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def condition_changes():
    """Damage-rate signal: how often items come back worse than they went out. Skips rows without condition_in."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    group_by = request.args.get("group_by", "asset")
    if group_by not in stats_service.VALID_GROUP_BY:
        return (
            jsonify({"error": f"group_by must be one of: {', '.join(stats_service.VALID_GROUP_BY)}"}),
            400,
        )

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.condition_changes(conn, limit, offset, group_by, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


# # # SUMMARY ---------------------------------------------------------------


@stats_bp.route("/stats/summary")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def stats_summary():
    """KPI row for Reports; overdue/damage rates share definitions with the dedicated endpoints."""
    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.stats_summary(conn, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


# # # STAFF ACTIVITY -------------------------------------------------------
# Admin-only; framed as workload coverage, not a leaderboard, sorted alphabetically.


@stats_bp.route("/stats/staff/activity")
@roles_required("admin")
@limiter.limit("20 per minute")
def staff_activity():
    """One row per active staff member: checkouts, returns, logged actions, avg session duration."""
    since = request.args.get("since")
    until = request.args.get("until")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return error
    if until:
        error = validate_date_string(until, "until")
        if error:
            return error

    conn = get_db_connection()
    try:
        result = stats_service.staff_activity(conn, since, until)
    finally:
        conn.close()

    return jsonify(result)


# # # TOOL KEEPER "MY ACTIVITY" -------------------------------------------
# Every query filters to g.staff["id"], so nobody sees anyone else's activity here.


@stats_bp.route("/stats/my/checkouts")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_checkouts():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = stats_service.parse_date_range(request.args, column="transactions.time_out")
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.my_checkouts(conn, g.staff["id"], limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/my/returns")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_returns():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = stats_service.parse_date_range(request.args, column="transactions.time_in")
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.my_returns(conn, g.staff["id"], limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/my/overdue-handled")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_overdue_handled():
    """This staff member's checkouts that ended up overdue, same definition as overdue_frequency()."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = stats_service.parse_date_range(request.args, column="transactions.time_out")
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.my_overdue_handled(conn, g.staff["id"], limit, offset, date_filter, date_params)
    finally:
        conn.close()

    return jsonify(result)


# # # LAYOUT (customizable/draggable widget order + visibility) -----------


@stats_bp.route("/stats/layout")
@roles_required("admin", "in_charge", "tool_keeper")
def get_layout():
    return jsonify(
        {
            "layout": get_report_layout(g.staff["id"], g.staff["role"]),
            "widgets": [
                {"id": w["id"], "title": w["title"], "type": w["type"]}
                for w in widgets_for_role(g.staff["role"])
            ],
        }
    )


@stats_bp.route("/stats/layout", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
def post_layout():
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("layout"), list):
        return jsonify({"error": "Expected {\"layout\": [...]}"}), 400
    saved = save_report_layout(g.staff["id"], g.staff["role"], data["layout"])
    return jsonify({"layout": saved})


@stats_bp.route("/stats/layout/reset", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
def post_layout_reset():
    reset_report_layout(g.staff["id"])
    return jsonify({"layout": get_report_layout(g.staff["id"], g.staff["role"])})


# # # EXPORT --------------------------------------------------------------


@stats_bp.route("/stats/export")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("10 per minute")
def export_stats():
    """Exports chosen report sections as xlsx/csv; a tampered `sections` id is silently dropped, not a 403."""
    role = g.staff["role"]
    allowed_ids = {w["id"] for w in widgets_for_role(role)}

    requested = request.args.get("sections")
    if requested:
        section_ids = [s for s in requested.split(",") if s in allowed_ids]
    else:
        section_ids = list(allowed_ids)
    # Keep registry order regardless of request order; unknown ids are already excluded.
    section_ids = [w["id"] for w in WIDGETS if w["id"] in section_ids]

    if not section_ids:
        return jsonify({"error": "No exportable sections selected"}), 400

    export_format = request.args.get("format", "xlsx")
    if export_format not in ("xlsx", "csv"):
        return jsonify({"error": "format must be xlsx or csv"}), 400

    date_filter, date_params, error = stats_service.parse_date_range(request.args)
    if error:
        return error

    loan_group_by = request.args.get("loan_duration_group_by", "asset")
    condition_group_by = request.args.get("condition_changes_group_by", "asset")
    if loan_group_by not in stats_service.VALID_GROUP_BY:
        loan_group_by = "asset"
    if condition_group_by not in stats_service.VALID_GROUP_BY:
        condition_group_by = "asset"

    ctx = {
        "date_filter": date_filter,
        "date_params": date_params,
        "where_sql": f"WHERE {date_filter}" if date_filter else "",
        "since": request.args.get("since"),
        "until": request.args.get("until"),
        "loan_group_by": loan_group_by,
        "condition_group_by": condition_group_by,
        "staff_id": g.staff["id"],
    }

    conn = get_db_connection()
    try:
        sections = stats_service.build_export_sections(conn, section_ids, ctx)
    finally:
        conn.close()

    basename = stats_service.export_basename()

    if export_format == "xlsx":
        response = make_response(stats_service.export_workbook_bytes(sections))
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = f"attachment; filename={basename}.xlsx"
        return response

    # CSV: one section downloads as a plain .csv; multiple get zipped, one file each.
    if len(sections) == 1:
        response = make_response(stats_service.export_single_csv_bytes(sections[0]))
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename={basename}.csv"
        return response

    response = make_response(stats_service.export_zip_bytes(sections))
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename={basename}.zip"
    return response
