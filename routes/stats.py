"""Thin HTTP layer para sa /stats/* endpoints (Reports tab's data + layout
+ export) - business logic naa sa services/stats_service.py ug
services/monitoring_stats_service.py."""

from flask import Blueprint, jsonify, request, make_response, g
from database import get_db_connection
from services.auth_service import roles_required
from services.settings_service import is_feature_enabled, get_feature_label
from services.report_widgets import WIDGETS, widgets_for_role
from services.report_layout_service import get_report_layout, save_report_layout, reset_report_layout
from services import stats_service, monitoring_stats_service
from utils.validators import validate_date_string, parse_pagination_params
from extensions import limiter

stats_bp = Blueprint("stats", __name__)


@stats_bp.before_request
def _block_if_reports_disabled():
    """Mo-403 sa tanan /stats/* endpoints kung naka-disable ang reports
    feature flag."""
    if not is_feature_enabled("reports"):
        return jsonify({"error": f"{get_feature_label('reports')} is currently disabled"}), 403


# # # ASSET USAGE ---------------------------------------------------------


@stats_bp.route("/stats/assets/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_assets():
    """GET - top assets by borrow count, date-ranged. Delegates to
    stats_service.most_borrowed_assets()."""
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
    """GET - inverse sa most-borrowed, apil ang zero-transaction assets
    (LEFT JOIN). Delegates to stats_service.least_borrowed_assets()."""
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


@stats_bp.route("/stats/assets/valuation")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def asset_valuation():
    """GET - current book value snapshot, dili date-ranged - see
    asset_valuation_summary's docstring sa service."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.asset_valuation_table(conn, limit, offset)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/assets/maintenance-schedule")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def maintenance_schedule():
    """GET - upcoming/overdue preventive maintenance, dili date-ranged, same
    current-state-snapshot reasoning sa asset_valuation() above."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    conn = get_db_connection()
    try:
        result = stats_service.maintenance_schedule_table(conn, limit, offset)
    finally:
        conn.close()

    return jsonify(result)


@stats_bp.route("/stats/monitoring-summary")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def monitoring_summary():
    """GET - Reports' "Monitoring Activity" widget - compact metric-list
    version sa Monitoring's own Trends tab, date-ranged same way
    (since/until, reusing Reports' own date filter). 403 kung naka-disable
    ang monitoring feature, same reason ngano wala ni sa /stats/layout ana
    nga case - see get_layout/export_stats' filtering below."""
    if not is_feature_enabled("monitoring"):
        return jsonify({"error": f"{get_feature_label('monitoring')} is currently disabled"}), 403

    conn = get_db_connection()
    try:
        results, error = monitoring_stats_service.get_summary(conn, request.args)
        if error:
            return error
    finally:
        conn.close()

    return jsonify({"results": results})


@stats_bp.route("/stats/categories/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_categories():
    """GET - top categories by borrow count, date-ranged. Delegates to
    stats_service.most_borrowed_categories()."""
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
    """GET - average hours between borrow ug return, returned transactions
    lang. group_by=asset or category."""
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
    """GET - members sorted by borrow count, date-ranged. Delegates to
    stats_service.top_borrowers()."""
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
    """GET - repeat-lateness history, distinct ni sa members.is_restricted.
    Split ang overdue_count ug still_open_count."""
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
    """GET - damage-rate signal: unsa ka often mo-balik ang items nga mas
    grabe ang condition sa dihang gi-borrow. Ma-skip ang rows nga walay
    condition_in."""
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
    """GET - KPI row para sa Reports; ang overdue/damage rates share ra ug
    definitions sa dedicated endpoints (overdue_frequency, condition_changes)."""
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
    """GET - admin-only, usa ka row per active staff member: checkouts,
    returns, logged actions, avg session duration."""
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
    """GET - "My Activity" widget: this staff member's own checkouts ra
    (scoped sa g.staff["id"]). Delegates to stats_service.my_checkouts()."""
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
    """GET - "My Activity" widget: this staff member's own returns ra
    (scoped sa g.staff["id"]). Delegates to stats_service.my_returns()."""
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
    """GET - this staff member's checkouts nga na-overdue, same definition
    sa overdue_frequency()."""
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


def _feature_disabled_widget_ids():
    """Widget ids nga i-hide regardless sa role, kay naka-disable ang
    feature nga gina-summarize - walay pulos ang monitoring_activity (ug
    mo-403 ang iyang own endpoint) kung off ang monitoring, kay wala pa
    man per-widget feature-flag field sa widget catalog para ma-check ni
    generically."""
    hidden = set()
    if not is_feature_enabled("monitoring"):
        hidden.add("monitoring_activity")
    return hidden


@stats_bp.route("/stats/layout")
@roles_required("admin", "in_charge", "tool_keeper")
def get_layout():
    """GET - current staff's saved widget order/visibility para sa
    Reports page, filtered sa feature-disabled widgets."""
    hidden_ids = _feature_disabled_widget_ids()
    return jsonify(
        {
            "layout": [entry for entry in get_report_layout(g.staff["id"], g.staff["role"]) if entry["widget_id"] not in hidden_ids],
            "widgets": [
                {"id": w["id"], "title": w["title"], "type": w["type"]}
                for w in widgets_for_role(g.staff["role"])
                if w["id"] not in hidden_ids
            ],
        }
    )


@stats_bp.route("/stats/layout", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
def post_layout():
    """POST - i-save ang custom widget order/visibility sa report_layout_service.
    Body: {"layout": [...]}."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("layout"), list):
        return jsonify({"error": "Expected {\"layout\": [...]}"}), 400
    saved = save_report_layout(g.staff["id"], g.staff["role"], data["layout"])
    return jsonify({"layout": saved})


@stats_bp.route("/stats/layout/reset", methods=["POST"])
@roles_required("admin", "in_charge", "tool_keeper")
def post_layout_reset():
    """POST - i-reset ang widget layout balik sa default order/visibility."""
    reset_report_layout(g.staff["id"])
    return jsonify({"layout": get_report_layout(g.staff["id"], g.staff["role"])})


# # # EXPORT --------------------------------------------------------------


@stats_bp.route("/stats/export")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("10 per minute")
def export_stats():
    """GET - i-export ang gipili nga report sections as xlsx/csv (or zip kung
    multiple csv). Tampered `sections` id kay silently dropped ra, dili 403."""
    role = g.staff["role"]
    allowed_ids = {w["id"] for w in widgets_for_role(role)} - _feature_disabled_widget_ids()

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
