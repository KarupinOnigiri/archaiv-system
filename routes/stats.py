from flask import Blueprint, jsonify, request, make_response, g
from database import get_db_connection
from services.auth_service import roles_required
from services.settings_service import is_feature_enabled, get_feature_label
from services.report_widgets import WIDGETS, widgets_for_role, is_widget_allowed
from services.report_layout_service import get_report_layout, save_report_layout, reset_report_layout
from utils.validators import validate_date_string, parse_pagination_params
from utils.exports import sanitize_export_filename
from utils.formatters import full_name
from extensions import limiter
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter
import csv
import io
import zipfile
from datetime import datetime

stats_bp = Blueprint("stats", __name__)


@stats_bp.before_request
def _block_if_reports_disabled():
    if not is_feature_enabled("reports"):
        return jsonify({"error": f"{get_feature_label('reports')} is currently disabled"}), 403

VALID_GROUP_BY = ("asset", "category")
# Exports go well past the on-screen top-10 preview, but still capped so a huge
# install's full history doesn't become what a spreadsheet download waits on.
EXPORT_ROW_LIMIT = 500


def _parse_date_range(args, column="transactions.time_out"):
    """Parses optional since/until query params filtering on the given
    column. Returns (sql_fragment, params, error_response). sql_fragment
    is '' if neither is given (no filtering applied)."""
    since = args.get("since")
    until = args.get("until")

    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, None, error
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, None, error

    clauses = []
    params = []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} <= ?")
        params.append(until)

    return " AND ".join(clauses), params, None


def _condition_rank_case(column):
    """Severity ranking (functional < for_repair < inoperable < disposed) for
    whichever column is passed in, shared with stats_summary() so both use
    the same "worse" definition."""
    return f"""
        CASE {column}
            WHEN 'functional' THEN 1
            WHEN 'for_repair' THEN 2
            WHEN 'inoperable' THEN 3
            WHEN 'disposed' THEN 4
        END
    """


# # # ASSET USAGE ---------------------------------------------------------


@stats_bp.route("/stats/assets/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_assets():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_sql = f"WHERE {date_filter}" if date_filter else ""

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT assets.id
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            {where_sql}
            GROUP BY assets.id
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT assets.id, assets.asset_id, assets.name, categories.name AS category,
               COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        GROUP BY assets.id
        ORDER BY borrow_count DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
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


@stats_bp.route("/stats/assets/least-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def least_borrowed_assets():
    """Inverse of most-borrowed, including assets with zero transactions.
    LEFT JOIN keeps those visible; the date filter has to sit in the JOIN
    condition, not WHERE, or it'd drop every zero-transaction asset."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    join_filter = f"AND {date_filter}" if date_filter else ""

    conn = get_db_connection()

    total_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM assets WHERE deleted_at IS NULL"
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT assets.id, assets.asset_id, assets.name, categories.name AS category,
               COUNT(transactions.id) AS borrow_count
        FROM assets
        LEFT JOIN transactions ON transactions.asset_id = assets.id {join_filter}
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.deleted_at IS NULL
        GROUP BY assets.id
        ORDER BY borrow_count ASC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
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


@stats_bp.route("/stats/categories/most-borrowed")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def most_borrowed_categories():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_sql = f"WHERE {date_filter}" if date_filter else ""

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT COALESCE(categories.name, 'Uncategorized') AS category
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            LEFT JOIN categories ON assets.category_id = categories.id
            {where_sql}
            GROUP BY category
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT COALESCE(categories.name, 'Uncategorized') AS category,
               COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        GROUP BY category
        ORDER BY borrow_count DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
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


@stats_bp.route("/stats/loan-duration")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def loan_duration():
    """Average hours between borrow and return. Only counts completed
    (returned) transactions — an asset still checked out has no
    meaningful duration yet. group_by=asset (default) or group_by=category."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    group_by = request.args.get("group_by", "asset")
    if group_by not in VALID_GROUP_BY:
        return (
            jsonify({"error": f"group_by must be one of: {', '.join(VALID_GROUP_BY)}"}),
            400,
        )

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_clauses = [
        "transactions.status = 'returned'",
        "transactions.time_in IS NOT NULL",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    if group_by == "asset":
        select_group = "assets.id AS group_id, assets.asset_id, assets.name"
        group_clause = "assets.id"
        count_group_col = "assets.id"
    else:
        select_group = "COALESCE(categories.name, 'Uncategorized') AS category"
        group_clause = "category"
        count_group_col = "COALESCE(categories.name, 'Uncategorized')"

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT {count_group_col} AS grp
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            LEFT JOIN categories ON assets.category_id = categories.id
            {where_sql}
            GROUP BY grp
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT {select_group},
               ROUND(AVG(
                   (julianday(transactions.time_in) - julianday(transactions.time_out)) * 24
               ), 2) AS avg_loan_hours
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        GROUP BY {group_clause}
        ORDER BY avg_loan_hours DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "group_by": group_by,
            "results": [dict(row) for row in rows],
        }
    )


# # # MEMBER BEHAVIOR ------------------------------------------------------


@stats_bp.route("/stats/members/top-borrowers")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def top_borrowers():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_sql = f"WHERE {date_filter}" if date_filter else ""

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT members.id
            FROM transactions
            JOIN members ON transactions.member_id = members.id
            {where_sql}
            GROUP BY members.id
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT members.id, members.member_code, members.first_name, members.last_name,
               members.middle_name, members.suffix, departments.name AS department,
               COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN members ON transactions.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        {where_sql}
        GROUP BY members.id
        ORDER BY borrow_count DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
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


@stats_bp.route("/stats/members/overdue-frequency")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def overdue_frequency():
    """Counts how many times each member went overdue (returned late, or
    still open and past due). This is repeat-lateness history, not the
    same thing as members.is_restricted (current status only). Returns
    both overdue_count and still_open_count so you can tell how much is
    in-progress vs. actually resolved late."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_clauses = [
        "transactions.due_date IS NOT NULL",
        "COALESCE(transactions.time_in, datetime('now', 'localtime')) > transactions.due_date",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT members.id
            FROM transactions
            JOIN members ON transactions.member_id = members.id
            {where_sql}
            GROUP BY members.id
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT members.id, members.member_code, members.first_name, members.last_name,
               members.middle_name, members.suffix, departments.name AS department,
               COUNT(transactions.id) AS overdue_count,
               SUM(CASE WHEN transactions.status = 'borrowed' THEN 1 ELSE 0 END)
                   AS still_open_count
        FROM transactions
        JOIN members ON transactions.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        {where_sql}
        GROUP BY members.id
        ORDER BY overdue_count DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
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


# # # CONDITION / DAMAGE ---------------------------------------------------


@stats_bp.route("/stats/assets/condition-changes")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def condition_changes():
    """Damage-rate signal: how often assets/categories come back worse than
    they went out, using the severity ranking since SQLite has no native
    ordering for condition strings. Skips old rows without condition_in."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error

    group_by = request.args.get("group_by", "asset")
    if group_by not in VALID_GROUP_BY:
        return (
            jsonify({"error": f"group_by must be one of: {', '.join(VALID_GROUP_BY)}"}),
            400,
        )

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_clauses = [
        "transactions.status = 'returned'",
        "transactions.condition_in IS NOT NULL",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    if group_by == "asset":
        select_group = "assets.id AS group_id, assets.asset_id, assets.name"
        group_clause = "assets.id"
        count_group_col = "assets.id"
    else:
        select_group = "COALESCE(categories.name, 'Uncategorized') AS category"
        group_clause = "category"
        count_group_col = "COALESCE(categories.name, 'Uncategorized')"

    out_rank = _condition_rank_case("transactions.condition_out")
    in_rank = _condition_rank_case("transactions.condition_in")

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT {count_group_col} AS grp
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            LEFT JOIN categories ON assets.category_id = categories.id
            {where_sql}
            GROUP BY grp
            HAVING COUNT(transactions.id) > 0
        )
        """,
        date_params,
    ).fetchone()["cnt"]

    rows = conn.execute(
        f"""
        SELECT {select_group},
               COUNT(transactions.id) AS completed_returns,
               SUM(
                   CASE WHEN ({in_rank}) > ({out_rank}) THEN 1 ELSE 0 END
               ) AS worsened_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {where_sql}
        GROUP BY {group_clause}
        HAVING completed_returns > 0
        ORDER BY worsened_count DESC
        LIMIT ? OFFSET ?
        """,
        (*date_params, limit, offset),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
            "group_by": group_by,
            "results": [dict(row) for row in rows],
        }
    )


# # # SUMMARY ---------------------------------------------------------------


@stats_bp.route("/stats/summary")
@roles_required("admin", "in_charge")
@limiter.limit("20 per minute")
def stats_summary():
    """KPI row for the Reports page: total checkouts, distinct assets/members,
    plus overdue and damage rates. Rates use the exact same definitions as
    overdue_frequency() and condition_changes() so the numbers can't drift."""
    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    where_sql = f"WHERE {date_filter}" if date_filter else ""

    conn = get_db_connection()

    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS total_checkouts,
               COUNT(DISTINCT asset_id) AS unique_assets,
               COUNT(DISTINCT member_id) AS unique_borrowers
        FROM transactions
        {where_sql}
        """,
        date_params,
    ).fetchone()

    # Same "overdue" definition as overdue_frequency(); rate is out of
    # transactions that had a due date (no due date = never at risk).
    overdue_where = ["due_date IS NOT NULL"]
    if date_filter:
        overdue_where.append(date_filter)
    overdue_row = conn.execute(
        f"""
        SELECT COUNT(*) AS with_due_date,
               SUM(
                   CASE WHEN COALESCE(time_in, datetime('now', 'localtime')) > due_date THEN 1 ELSE 0 END
               ) AS overdue_count
        FROM transactions
        WHERE {' AND '.join(overdue_where)}
        """,
        date_params,
    ).fetchone()
    overdue_rate = (
        round(100 * overdue_row["overdue_count"] / overdue_row["with_due_date"], 1)
        if overdue_row["with_due_date"]
        else 0
    )

    # Same "worsened" definition as condition_changes().
    damage_where = ["transactions.status = 'returned'", "transactions.condition_in IS NOT NULL"]
    if date_filter:
        damage_where.append(date_filter)
    out_rank = _condition_rank_case("transactions.condition_out")
    in_rank = _condition_rank_case("transactions.condition_in")
    damage_row = conn.execute(
        f"""
        SELECT COUNT(*) AS completed_returns,
               SUM(CASE WHEN ({in_rank}) > ({out_rank}) THEN 1 ELSE 0 END) AS worsened_count
        FROM transactions
        WHERE {' AND '.join(damage_where)}
        """,
        date_params,
    ).fetchone()
    damage_rate = (
        round(100 * damage_row["worsened_count"] / damage_row["completed_returns"], 1)
        if damage_row["completed_returns"]
        else 0
    )

    conn.close()

    return jsonify(
        {
            "total_checkouts": totals["total_checkouts"],
            "unique_assets": totals["unique_assets"],
            "unique_borrowers": totals["unique_borrowers"],
            "overdue_rate": overdue_rate,
            "damage_rate": damage_rate,
        }
    )


# # # STAFF ACTIVITY -------------------------------------------------------
# Admin-only, unlike the other reports here -- staff oversight is admin-only everywhere else.
# Framed as workload/coverage, not a leaderboard: lists every active staff member
# including zero-activity ones, sorted alphabetically rather than by count.


@stats_bp.route("/stats/staff/activity")
@roles_required("admin")
@limiter.limit("20 per minute")
def staff_activity():
    """One row per active staff member: checkouts, returns, logged actions,
    and avg session duration, each date-filterable. All pulled from existing
    tables, nothing new tracked."""
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

    def _range_clause(column):
        clauses = []
        params = []
        if since:
            clauses.append(f"{column} >= ?")
            params.append(since)
        if until:
            clauses.append(f"{column} <= ?")
            params.append(until)
        return ("AND " + " AND ".join(clauses)) if clauses else "", params

    checkout_clause, checkout_params = _range_clause("time_out")
    return_clause, return_params = _range_clause("time_in")
    audit_clause, audit_params = _range_clause("created_at")
    session_clause, session_params = _range_clause("created_at")

    conn = get_db_connection()
    rows = conn.execute(
        f"""
        SELECT staff.id, staff.username, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               COALESCE(co.cnt, 0) AS checkouts_processed,
               COALESCE(ret.cnt, 0) AS returns_processed,
               COALESCE(al.cnt, 0) AS audit_actions,
               ROUND(cs.avg_minutes, 1) AS avg_session_minutes
        FROM staff
        JOIN members ON staff.member_id = members.id
        LEFT JOIN (
            SELECT borrowed_by_staff_id AS staff_id, COUNT(*) AS cnt
            FROM transactions
            WHERE borrowed_by_staff_id IS NOT NULL {checkout_clause}
            GROUP BY borrowed_by_staff_id
        ) co ON co.staff_id = staff.id
        LEFT JOIN (
            SELECT returned_by_staff_id AS staff_id, COUNT(*) AS cnt
            FROM transactions
            WHERE returned_by_staff_id IS NOT NULL {return_clause}
            GROUP BY returned_by_staff_id
        ) ret ON ret.staff_id = staff.id
        LEFT JOIN (
            SELECT staff_id, COUNT(*) AS cnt
            FROM audit_log
            WHERE staff_id IS NOT NULL {audit_clause}
            GROUP BY staff_id
        ) al ON al.staff_id = staff.id
        LEFT JOIN (
            SELECT staff_id, AVG((julianday(completed_at) - julianday(created_at)) * 24 * 60) AS avg_minutes
            FROM checkout_sessions
            WHERE status = 'completed' AND staff_id IS NOT NULL AND completed_at IS NOT NULL {session_clause}
            GROUP BY staff_id
        ) cs ON cs.staff_id = staff.id
        WHERE staff.is_active = 1
        ORDER BY members.last_name COLLATE NOCASE, members.first_name COLLATE NOCASE
        """,
        (*checkout_params, *return_params, *audit_params, *session_params),
    ).fetchall()
    conn.close()

    return jsonify(
        {
            "results": [
                {
                    "id": row["id"],
                    "username": row["username"],
                    "role": row["role"],
                    "name": full_name(
                        row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
                    ),
                    "checkouts_processed": row["checkouts_processed"],
                    "returns_processed": row["returns_processed"],
                    "audit_actions": row["audit_actions"],
                    "avg_session_minutes": row["avg_session_minutes"],
                }
                for row in rows
            ]
        }
    )


# # # TOOL KEEPER "MY ACTIVITY" -------------------------------------------
# Scoped to the caller's own activity only -- every query below filters to g.staff["id"]
# unconditionally, so nobody sees anyone else's checkouts/returns through these endpoints.


@stats_bp.route("/stats/my/checkouts")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_checkouts():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = _parse_date_range(request.args, column="transactions.time_out")
    if error:
        return error

    where_clauses = ["transactions.borrowed_by_staff_id = ?"]
    params = [g.staff["id"]]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM transactions {where_sql}", params
    ).fetchone()["cnt"]
    rows = conn.execute(
        f"""
        SELECT transactions.id, assets.asset_id, assets.name AS asset_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               transactions.time_out, transactions.status
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        {where_sql}
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
            "results": [
                {
                    "id": r["id"],
                    "asset_id": r["asset_id"],
                    "asset_name": r["asset_name"],
                    "member_name": full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
                    "time_out": r["time_out"],
                    "status": r["status"],
                }
                for r in rows
            ],
        }
    )


@stats_bp.route("/stats/my/returns")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_returns():
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = _parse_date_range(request.args, column="transactions.time_in")
    if error:
        return error

    where_clauses = ["transactions.returned_by_staff_id = ?"]
    params = [g.staff["id"]]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM transactions {where_sql}", params
    ).fetchone()["cnt"]
    rows = conn.execute(
        f"""
        SELECT transactions.id, assets.asset_id, assets.name AS asset_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               transactions.time_in, transactions.condition_in
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        {where_sql}
        ORDER BY transactions.time_in DESC
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
            "results": [
                {
                    "id": r["id"],
                    "asset_id": r["asset_id"],
                    "asset_name": r["asset_name"],
                    "member_name": full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
                    "time_in": r["time_in"],
                    "condition_in": r["condition_in"],
                }
                for r in rows
            ],
        }
    )


@stats_bp.route("/stats/my/overdue-handled")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("20 per minute")
def my_overdue_handled():
    """Transactions this staff member checked out that ended up overdue,
    same definition as overdue_frequency(), scoped to their own loans."""
    limit, offset, error = parse_pagination_params(request.args)
    if error:
        return error
    date_filter, date_params, error = _parse_date_range(request.args, column="transactions.time_out")
    if error:
        return error

    where_clauses = [
        "transactions.borrowed_by_staff_id = ?",
        "transactions.due_date IS NOT NULL",
        "COALESCE(transactions.time_in, datetime('now', 'localtime')) > transactions.due_date",
    ]
    params = [g.staff["id"]]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM transactions {where_sql}", params
    ).fetchone()["cnt"]
    rows = conn.execute(
        f"""
        SELECT transactions.id, assets.asset_id, assets.name AS asset_name,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               transactions.due_date, transactions.time_in, transactions.status
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        JOIN members ON transactions.member_id = members.id
        {where_sql}
        ORDER BY transactions.due_date DESC
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
            "results": [
                {
                    "id": r["id"],
                    "asset_id": r["asset_id"],
                    "asset_name": r["asset_name"],
                    "member_name": full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
                    "due_date": r["due_date"],
                    "time_in": r["time_in"],
                    "status": r["status"],
                }
                for r in rows
            ],
        }
    )


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


def _autofit(ws, headers, rows_as_lists):
    """Same autofit as routes/staff.py's export_staff: width = widest cell
    in the column, capped so one long outlier can't blow out the sheet."""
    for i, header in enumerate(headers):
        longest = len(str(header))
        for row in rows_as_lists:
            cell = row[i]
            longest = max(longest, len(str(cell)) if cell is not None else 0)
        ws.column_dimensions[get_column_letter(i + 1)].width = min(max(longest + 2, 10), 40)


def _write_sheet(wb, title, headers, rows_as_lists):
    ws = wb.create_sheet(title=title[:31])  # Excel sheet-name length limit
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for row in rows_as_lists:
        ws.append(row)
    _autofit(ws, headers, rows_as_lists)
    ws.freeze_panes = "A2"


# -- Per-widget (headers, rows_as_lists) builders --------------------------
# Same query/shape as the matching on-screen report card, just returning
# plain (title, headers, rows) so export_stats() can format it as CSV/XLSX.


def _section_summary(conn, ctx):
    date_filter, date_params, where_sql = ctx["date_filter"], ctx["date_params"], ctx["where_sql"]
    totals = conn.execute(
        f"""
        SELECT COUNT(*) AS total_checkouts, COUNT(DISTINCT asset_id) AS unique_assets,
               COUNT(DISTINCT member_id) AS unique_borrowers
        FROM transactions {where_sql}
        """,
        date_params,
    ).fetchone()
    overdue_where = ["due_date IS NOT NULL"] + ([date_filter] if date_filter else [])
    overdue_row = conn.execute(
        f"""
        SELECT COUNT(*) AS with_due_date,
               SUM(CASE WHEN COALESCE(time_in, datetime('now', 'localtime')) > due_date THEN 1 ELSE 0 END) AS overdue_count
        FROM transactions WHERE {' AND '.join(overdue_where)}
        """,
        date_params,
    ).fetchone()
    overdue_rate = round(100 * overdue_row["overdue_count"] / overdue_row["with_due_date"], 1) if overdue_row["with_due_date"] else 0
    damage_where = ["transactions.status = 'returned'", "transactions.condition_in IS NOT NULL"] + ([date_filter] if date_filter else [])
    out_rank, in_rank = _condition_rank_case("transactions.condition_out"), _condition_rank_case("transactions.condition_in")
    damage_row = conn.execute(
        f"""
        SELECT COUNT(*) AS completed_returns, SUM(CASE WHEN ({in_rank}) > ({out_rank}) THEN 1 ELSE 0 END) AS worsened_count
        FROM transactions WHERE {' AND '.join(damage_where)}
        """,
        date_params,
    ).fetchone()
    damage_rate = round(100 * damage_row["worsened_count"] / damage_row["completed_returns"], 1) if damage_row["completed_returns"] else 0

    since_label = ctx["since"] or "all time"
    until_label = ctx["until"] or "present"
    return (
        "Summary",
        ["Metric", "Value"],
        [
            ["Date range", f"{since_label} to {until_label}"],
            ["Total checkouts", totals["total_checkouts"]],
            ["Unique assets borrowed", totals["unique_assets"]],
            ["Unique borrowers", totals["unique_borrowers"]],
            ["Overdue rate", f"{overdue_rate}%"],
            ["Damage rate", f"{damage_rate}%"],
        ],
    )


def _section_most_borrowed_assets(conn, ctx):
    rows = conn.execute(
        f"""
        SELECT assets.asset_id, assets.name, categories.name AS category, COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {ctx['where_sql']}
        GROUP BY assets.id ORDER BY borrow_count DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Most Borrowed Assets",
        ["Asset Code", "Name", "Category", "Borrows"],
        [[r["asset_id"], r["name"], r["category"] or "Uncategorized", r["borrow_count"]] for r in rows],
    )


def _section_most_borrowed_categories(conn, ctx):
    rows = conn.execute(
        f"""
        SELECT COALESCE(categories.name, 'Uncategorized') AS category, COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        {ctx['where_sql']}
        GROUP BY category ORDER BY borrow_count DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Most Borrowed Categories",
        ["Category", "Borrows"],
        [[r["category"], r["borrow_count"]] for r in rows],
    )


def _section_least_borrowed_assets(conn, ctx):
    join_filter = f"AND {ctx['date_filter']}" if ctx["date_filter"] else ""
    rows = conn.execute(
        f"""
        SELECT assets.asset_id, assets.name, categories.name AS category, COUNT(transactions.id) AS borrow_count
        FROM assets
        LEFT JOIN transactions ON transactions.asset_id = assets.id {join_filter}
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE assets.deleted_at IS NULL
        GROUP BY assets.id ORDER BY borrow_count ASC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Least Borrowed Assets",
        ["Asset Code", "Name", "Category", "Borrows"],
        [[r["asset_id"], r["name"], r["category"] or "Uncategorized", r["borrow_count"]] for r in rows],
    )


def _section_avg_loan_duration(conn, ctx):
    date_filter = ctx["date_filter"]
    group_by = ctx["loan_group_by"]
    ld_where = ["transactions.status = 'returned'", "transactions.time_in IS NOT NULL"] + ([date_filter] if date_filter else [])
    if group_by == "asset":
        select_group, group_clause = "assets.asset_id, assets.name", "assets.id"
        headers = ["Asset Code", "Name", "Avg Hours"]
    else:
        select_group, group_clause = "COALESCE(categories.name, 'Uncategorized') AS category", "category"
        headers = ["Category", "Avg Hours"]
    rows = conn.execute(
        f"""
        SELECT {select_group},
               ROUND(AVG((julianday(transactions.time_in) - julianday(transactions.time_out)) * 24), 2) AS avg_loan_hours
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE {' AND '.join(ld_where)}
        GROUP BY {group_clause} ORDER BY avg_loan_hours DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Avg Loan Duration",
        headers,
        [([r["asset_id"], r["name"], r["avg_loan_hours"]] if group_by == "asset"
          else [r["category"], r["avg_loan_hours"]]) for r in rows],
    )


def _section_top_borrowers(conn, ctx):
    rows = conn.execute(
        f"""
        SELECT members.member_code, members.first_name, members.last_name, members.middle_name, members.suffix,
               departments.name AS department, COUNT(transactions.id) AS borrow_count
        FROM transactions
        JOIN members ON transactions.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        {ctx['where_sql']}
        GROUP BY members.id ORDER BY borrow_count DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Top Borrowers",
        ["Member Code", "Name", "Department", "Borrows"],
        [[r["member_code"], full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
          r["department"], r["borrow_count"]] for r in rows],
    )


def _section_overdue_frequency(conn, ctx):
    date_filter = ctx["date_filter"]
    od_where = ["transactions.due_date IS NOT NULL",
                "COALESCE(transactions.time_in, datetime('now', 'localtime')) > transactions.due_date"] + ([date_filter] if date_filter else [])
    rows = conn.execute(
        f"""
        SELECT members.member_code, members.first_name, members.last_name, members.middle_name, members.suffix,
               departments.name AS department, COUNT(transactions.id) AS overdue_count,
               SUM(CASE WHEN transactions.status = 'borrowed' THEN 1 ELSE 0 END) AS still_open_count
        FROM transactions
        JOIN members ON transactions.member_id = members.id
        JOIN departments ON members.department_id = departments.id
        WHERE {' AND '.join(od_where)}
        GROUP BY members.id ORDER BY overdue_count DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Overdue Frequency",
        ["Member Code", "Name", "Department", "Overdue", "Still Open"],
        [[r["member_code"], full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
          r["department"], r["overdue_count"], r["still_open_count"]] for r in rows],
    )


def _section_condition_changes(conn, ctx):
    date_filter = ctx["date_filter"]
    group_by = ctx["condition_group_by"]
    cc_where = ["transactions.status = 'returned'", "transactions.condition_in IS NOT NULL"] + ([date_filter] if date_filter else [])
    out_rank, in_rank = _condition_rank_case("transactions.condition_out"), _condition_rank_case("transactions.condition_in")
    if group_by == "asset":
        select_group, group_clause = "assets.asset_id, assets.name", "assets.id"
        headers = ["Asset Code", "Name", "Completed Returns", "Worsened"]
    else:
        select_group, group_clause = "COALESCE(categories.name, 'Uncategorized') AS category", "category"
        headers = ["Category", "Completed Returns", "Worsened"]
    rows = conn.execute(
        f"""
        SELECT {select_group}, COUNT(transactions.id) AS completed_returns,
               SUM(CASE WHEN ({in_rank}) > ({out_rank}) THEN 1 ELSE 0 END) AS worsened_count
        FROM transactions
        JOIN assets ON transactions.asset_id = assets.id
        LEFT JOIN categories ON assets.category_id = categories.id
        WHERE {' AND '.join(cc_where)}
        GROUP BY {group_clause} HAVING completed_returns > 0 ORDER BY worsened_count DESC LIMIT ?
        """,
        (*ctx["date_params"], EXPORT_ROW_LIMIT),
    ).fetchall()
    return (
        "Condition Changes",
        headers,
        [([r["asset_id"], r["name"], r["completed_returns"], r["worsened_count"]] if group_by == "asset"
          else [r["category"], r["completed_returns"], r["worsened_count"]]) for r in rows],
    )


def _section_staff_activity(conn, ctx):
    date_filter, date_params = ctx["date_filter"], ctx["date_params"]
    checkout_clause = f"AND {date_filter.replace('transactions.time_out', 'time_out')}" if date_filter else ""
    return_clause = f"AND {date_filter.replace('transactions.time_out', 'time_in')}" if date_filter else ""
    audit_clause = f"AND {date_filter.replace('transactions.time_out', 'created_at')}" if date_filter else ""
    session_clause = audit_clause
    rows = conn.execute(
        f"""
        SELECT staff.username, staff.role,
               members.first_name, members.last_name, members.middle_name, members.suffix,
               COALESCE(co.cnt, 0) AS checkouts_processed,
               COALESCE(ret.cnt, 0) AS returns_processed,
               COALESCE(al.cnt, 0) AS audit_actions,
               ROUND(cs.avg_minutes, 1) AS avg_session_minutes
        FROM staff
        JOIN members ON staff.member_id = members.id
        LEFT JOIN (
            SELECT borrowed_by_staff_id AS staff_id, COUNT(*) AS cnt
            FROM transactions WHERE borrowed_by_staff_id IS NOT NULL {checkout_clause}
            GROUP BY borrowed_by_staff_id
        ) co ON co.staff_id = staff.id
        LEFT JOIN (
            SELECT returned_by_staff_id AS staff_id, COUNT(*) AS cnt
            FROM transactions WHERE returned_by_staff_id IS NOT NULL {return_clause}
            GROUP BY returned_by_staff_id
        ) ret ON ret.staff_id = staff.id
        LEFT JOIN (
            SELECT staff_id, COUNT(*) AS cnt
            FROM audit_log WHERE staff_id IS NOT NULL {audit_clause}
            GROUP BY staff_id
        ) al ON al.staff_id = staff.id
        LEFT JOIN (
            SELECT staff_id, AVG((julianday(completed_at) - julianday(created_at)) * 24 * 60) AS avg_minutes
            FROM checkout_sessions
            WHERE status = 'completed' AND staff_id IS NOT NULL AND completed_at IS NOT NULL {session_clause}
            GROUP BY staff_id
        ) cs ON cs.staff_id = staff.id
        WHERE staff.is_active = 1
        ORDER BY members.last_name COLLATE NOCASE, members.first_name COLLATE NOCASE
        """,
        (*date_params, *date_params, *date_params, *date_params),
    ).fetchall()
    return (
        "Staff Activity",
        ["Username", "Role", "Name", "Checkouts Processed", "Returns Processed", "Logged Actions", "Avg Session (min)"],
        [[r["username"], r["role"], full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
          r["checkouts_processed"], r["returns_processed"], r["audit_actions"], r["avg_session_minutes"]] for r in rows],
    )


def _my_activity_section(title, staff_col, date_col, extra_col, extra_header):
    """Factory for the three near-identical "my activity" export sections,
    mirroring my_checkouts/my_returns/my_overdue_handled above."""

    def builder(conn, ctx):
        rows = conn.execute(
            f"""
            SELECT assets.asset_id, assets.name AS asset_name,
                   members.first_name, members.last_name, members.middle_name, members.suffix,
                   transactions.{date_col} AS date_col, transactions.{extra_col} AS extra_col
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            JOIN members ON transactions.member_id = members.id
            WHERE transactions.{staff_col} = ?
            ORDER BY transactions.{date_col} DESC
            LIMIT ?
            """,
            (ctx["staff_id"], EXPORT_ROW_LIMIT),
        ).fetchall()
        return (
            title,
            ["Asset Code", "Asset Name", "Member", date_col.replace("_", " ").title(), extra_header],
            [[r["asset_id"], r["asset_name"],
              full_name(r["first_name"], r["last_name"], r["middle_name"], r["suffix"]),
              r["date_col"], r["extra_col"]] for r in rows],
        )

    return builder


SECTION_BUILDERS = {
    "summary": _section_summary,
    "most_borrowed_assets": _section_most_borrowed_assets,
    "most_borrowed_categories": _section_most_borrowed_categories,
    "least_borrowed_assets": _section_least_borrowed_assets,
    "avg_loan_duration": _section_avg_loan_duration,
    "top_borrowers": _section_top_borrowers,
    "overdue_frequency": _section_overdue_frequency,
    "condition_changes": _section_condition_changes,
    "staff_activity": _section_staff_activity,
    "my_checkouts": _my_activity_section("My Checkouts Processed", "borrowed_by_staff_id", "time_out", "status", "Status"),
    "my_returns": _my_activity_section("My Returns Processed", "returned_by_staff_id", "time_in", "condition_in", "Condition In"),
    "my_overdue_handled": _my_activity_section("My Overdue Handled", "borrowed_by_staff_id", "due_date", "status", "Status"),
}


def _rows_to_csv_bytes(headers, rows_as_lists):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows_as_lists)
    return buf.getvalue().encode("utf-8-sig")  # BOM so Excel opens UTF-8 CSVs correctly


@stats_bp.route("/stats/export")
@roles_required("admin", "in_charge", "tool_keeper")
@limiter.limit("10 per minute")
def export_stats():
    """Exports chosen report sections (`sections=`, comma-separated widget
    ids, defaults to everything the role can see) as xlsx or csv, using the
    same since/until range and group_by toggles as the Reports page.
    `sections` is filtered against the caller's actual role in
    report_widgets.py rather than trusted as-is, so a tampered request for
    e.g. "staff_activity" just gets silently dropped, not a 403."""
    role = g.staff["role"]
    allowed_ids = {w["id"] for w in widgets_for_role(role)}

    requested = request.args.get("sections")
    if requested:
        section_ids = [s for s in requested.split(",") if s in allowed_ids]
    else:
        section_ids = list(allowed_ids)
    # Keep registry order regardless of request order, and drop unknown ids
    # entirely (is_widget_allowed already excludes them from allowed_ids).
    section_ids = [w["id"] for w in WIDGETS if w["id"] in section_ids]

    if not section_ids:
        return jsonify({"error": "No exportable sections selected"}), 400

    export_format = request.args.get("format", "xlsx")
    if export_format not in ("xlsx", "csv"):
        return jsonify({"error": "format must be xlsx or csv"}), 400

    date_filter, date_params, error = _parse_date_range(request.args)
    if error:
        return error

    loan_group_by = request.args.get("loan_duration_group_by", "asset")
    condition_group_by = request.args.get("condition_changes_group_by", "asset")
    if loan_group_by not in VALID_GROUP_BY:
        loan_group_by = "asset"
    if condition_group_by not in VALID_GROUP_BY:
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
        sections = [(wid, *SECTION_BUILDERS[wid](conn, ctx)) for wid in section_ids]
    finally:
        conn.close()

    date_label = datetime.now().strftime("%Y%m%d")
    basename = sanitize_export_filename(None, f"reports_{date_label}")

    if export_format == "xlsx":
        wb = Workbook()
        wb.remove(wb.active)
        for _wid, title, headers, rows in sections:
            _write_sheet(wb, title, headers, rows)
        buf = io.BytesIO()
        wb.save(buf)
        response = make_response(buf.getvalue())
        response.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        response.headers["Content-Disposition"] = f"attachment; filename={basename}.xlsx"
        return response

    # CSV: one section downloads as a single .csv; multiple get zipped
    # (one .csv each) since they have different column shapes.
    if len(sections) == 1:
        _wid, title, headers, rows = sections[0]
        response = make_response(_rows_to_csv_bytes(headers, rows))
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = f"attachment; filename={basename}.csv"
        return response

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _wid, title, headers, rows in sections:
            safe_title = sanitize_export_filename(None, title)
            zf.writestr(f"{safe_title}.csv", _rows_to_csv_bytes(headers, rows))
    response = make_response(zip_buf.getvalue())
    response.headers["Content-Type"] = "application/zip"
    response.headers["Content-Disposition"] = f"attachment; filename={basename}.zip"
    return response
