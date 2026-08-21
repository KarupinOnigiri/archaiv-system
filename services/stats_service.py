"""Query-building and data-shaping for the Reports/Stats tab.

Every function here takes an open `conn` (and already-validated filter values) and
returns plain dicts/tuples ready for the route to jsonify or export — no Flask
objects, no request parsing. `routes/stats.py` stays a thin HTTP layer: parse
args, call here, jsonify/return.
"""

import csv
import io
import zipfile
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from utils.exports import sanitize_export_filename
from utils.formatters import full_name
from utils.validators import validate_date_string

VALID_GROUP_BY = ("asset", "category")
# Capped well past the top-10 preview, so a huge install's history isn't a slow download.
EXPORT_ROW_LIMIT = 500


def parse_date_range(args, column="transactions.time_out"):
    """Parses optional since/until params; sql_fragment is '' when neither is given."""
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


def condition_rank_case(column):
    """Severity ranking (functional < for_repair < inoperable < disposed), shared by several queries."""
    return f"""
        CASE {column}
            WHEN 'functional' THEN 1
            WHEN 'for_repair' THEN 2
            WHEN 'inoperable' THEN 3
            WHEN 'disposed' THEN 4
        END
    """


# # # ASSET USAGE ---------------------------------------------------------


def most_borrowed_assets(conn, limit, offset, date_filter, date_params):
    where_sql = f"WHERE {date_filter}" if date_filter else ""

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


def least_borrowed_assets(conn, limit, offset, date_filter, date_params):
    """Inverse of most-borrowed, including zero-transaction assets via LEFT JOIN."""
    join_filter = f"AND {date_filter}" if date_filter else ""

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


def most_borrowed_categories(conn, limit, offset, date_filter, date_params):
    where_sql = f"WHERE {date_filter}" if date_filter else ""

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


def _loan_duration_group(group_by):
    """Shared group-by SQL fragments for loan_duration() and the export section."""
    if group_by == "asset":
        return "assets.id AS group_id, assets.asset_id, assets.name", "assets.id", "assets.id"
    return (
        "COALESCE(categories.name, 'Uncategorized') AS category",
        "category",
        "COALESCE(categories.name, 'Uncategorized')",
    )


def loan_duration(conn, limit, offset, group_by, date_filter, date_params):
    """Average hours between borrow and return, returned transactions only."""
    where_clauses = [
        "transactions.status = 'returned'",
        "transactions.time_in IS NOT NULL",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    select_group, group_clause, count_group_col = _loan_duration_group(group_by)

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "group_by": group_by,
        "results": [dict(row) for row in rows],
    }


# # # MEMBER BEHAVIOR ------------------------------------------------------


def top_borrowers(conn, limit, offset, date_filter, date_params):
    where_sql = f"WHERE {date_filter}" if date_filter else ""

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


def overdue_frequency(conn, limit, offset, date_filter, date_params):
    """Repeat-lateness history, distinct from members.is_restricted. Splits overdue_count and still_open_count."""
    where_clauses = [
        "transactions.due_date IS NOT NULL",
        "COALESCE(transactions.time_in, datetime('now', 'localtime')) > transactions.due_date",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "results": [dict(row) for row in rows],
    }


# # # CONDITION / DAMAGE ---------------------------------------------------


def _condition_changes_group(group_by):
    if group_by == "asset":
        return "assets.id AS group_id, assets.asset_id, assets.name", "assets.id", "assets.id"
    return (
        "COALESCE(categories.name, 'Uncategorized') AS category",
        "category",
        "COALESCE(categories.name, 'Uncategorized')",
    )


def condition_changes(conn, limit, offset, group_by, date_filter, date_params):
    """Damage-rate signal: how often items come back worse than they went out. Skips rows without condition_in."""
    where_clauses = [
        "transactions.status = 'returned'",
        "transactions.condition_in IS NOT NULL",
    ]
    if date_filter:
        where_clauses.append(date_filter)
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

    select_group, group_clause, count_group_col = _condition_changes_group(group_by)
    out_rank = condition_rank_case("transactions.condition_out")
    in_rank = condition_rank_case("transactions.condition_in")

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

    return {
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "group_by": group_by,
        "results": [dict(row) for row in rows],
    }


# # # SUMMARY ---------------------------------------------------------------


def _overdue_rate(conn, date_filter, date_params):
    """Shared by stats_summary() and the export summary section."""
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
    if not overdue_row["with_due_date"]:
        return 0
    return round(100 * overdue_row["overdue_count"] / overdue_row["with_due_date"], 1)


def _damage_rate(conn, date_filter, date_params):
    """Shared by stats_summary() and the export summary section."""
    damage_where = ["transactions.status = 'returned'", "transactions.condition_in IS NOT NULL"]
    if date_filter:
        damage_where.append(date_filter)
    out_rank = condition_rank_case("transactions.condition_out")
    in_rank = condition_rank_case("transactions.condition_in")
    damage_row = conn.execute(
        f"""
        SELECT COUNT(*) AS completed_returns,
               SUM(CASE WHEN ({in_rank}) > ({out_rank}) THEN 1 ELSE 0 END) AS worsened_count
        FROM transactions
        WHERE {' AND '.join(damage_where)}
        """,
        date_params,
    ).fetchone()
    if not damage_row["completed_returns"]:
        return 0
    return round(100 * damage_row["worsened_count"] / damage_row["completed_returns"], 1)


def stats_summary(conn, date_filter, date_params):
    """KPI row for Reports; overdue/damage rates share definitions with the dedicated endpoints."""
    where_sql = f"WHERE {date_filter}" if date_filter else ""

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

    return {
        "total_checkouts": totals["total_checkouts"],
        "unique_assets": totals["unique_assets"],
        "unique_borrowers": totals["unique_borrowers"],
        "overdue_rate": _overdue_rate(conn, date_filter, date_params),
        "damage_rate": _damage_rate(conn, date_filter, date_params),
    }


# # # STAFF ACTIVITY -------------------------------------------------------
# Admin-only; framed as workload coverage, not a leaderboard, sorted alphabetically.


def _staff_activity_range_clause(column, since, until):
    clauses = []
    params = []
    if since:
        clauses.append(f"{column} >= ?")
        params.append(since)
    if until:
        clauses.append(f"{column} <= ?")
        params.append(until)
    return ("AND " + " AND ".join(clauses)) if clauses else "", params


def staff_activity(conn, since, until):
    """One row per active staff member: checkouts, returns, logged actions, avg session duration."""
    checkout_clause, checkout_params = _staff_activity_range_clause("time_out", since, until)
    return_clause, return_params = _staff_activity_range_clause("time_in", since, until)
    audit_clause, audit_params = _staff_activity_range_clause("created_at", since, until)
    session_clause, session_params = _staff_activity_range_clause("created_at", since, until)

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

    return {
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


# # # TOOL KEEPER "MY ACTIVITY" -------------------------------------------
# Every query filters to the calling staff id, so nobody sees anyone else's activity here.


def my_checkouts(conn, staff_id, limit, offset, date_filter, date_params):
    where_clauses = ["transactions.borrowed_by_staff_id = ?"]
    params = [staff_id]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

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

    return {
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


def my_returns(conn, staff_id, limit, offset, date_filter, date_params):
    where_clauses = ["transactions.returned_by_staff_id = ?"]
    params = [staff_id]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

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

    return {
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


def my_overdue_handled(conn, staff_id, limit, offset, date_filter, date_params):
    """This staff member's checkouts that ended up overdue, same definition as overdue_frequency()."""
    where_clauses = [
        "transactions.borrowed_by_staff_id = ?",
        "transactions.due_date IS NOT NULL",
        "COALESCE(transactions.time_in, datetime('now', 'localtime')) > transactions.due_date",
    ]
    params = [staff_id]
    if date_filter:
        where_clauses.append(date_filter)
        params += date_params
    where_sql = f"WHERE {' AND '.join(where_clauses)}"

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

    return {
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


# # # EXPORT --------------------------------------------------------------


def _autofit(ws, headers, rows_as_lists):
    """width = widest cell, capped against one outlier."""
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
# Same shape as the on-screen report card, returned plain for export_stats() to format.


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
    overdue_rate = _overdue_rate(conn, date_filter, date_params)
    damage_rate = _damage_rate(conn, date_filter, date_params)

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
    out_rank, in_rank = condition_rank_case("transactions.condition_out"), condition_rank_case("transactions.condition_in")
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
    """Factory for the three near-identical "my activity" export sections."""

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


def build_export_sections(conn, section_ids, ctx):
    """Runs each requested section's builder against conn/ctx; returns [(id, title, headers, rows), ...]."""
    return [(wid, *SECTION_BUILDERS[wid](conn, ctx)) for wid in section_ids]


def export_workbook_bytes(sections):
    """sections: [(id, title, headers, rows_as_lists), ...] -> .xlsx bytes."""
    wb = Workbook()
    wb.remove(wb.active)
    for _wid, title, headers, rows in sections:
        _write_sheet(wb, title, headers, rows)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_single_csv_bytes(section):
    """section: (id, title, headers, rows_as_lists) -> .csv bytes."""
    _wid, _title, headers, rows = section
    return _rows_to_csv_bytes(headers, rows)


def export_zip_bytes(sections):
    """sections: [(id, title, headers, rows_as_lists), ...] -> .zip bytes, one .csv per section."""
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for _wid, title, headers, rows in sections:
            safe_title = sanitize_export_filename(None, title)
            zf.writestr(f"{safe_title}.csv", _rows_to_csv_bytes(headers, rows))
    return zip_buf.getvalue()


def export_basename():
    date_label = datetime.now().strftime("%Y%m%d")
    return sanitize_export_filename(None, f"reports_{date_label}")
