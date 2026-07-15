from flask import Blueprint, jsonify, request
from database import get_db_connection
from services.auth_service import roles_required
from utils.validators import validate_date_string, parse_pagination_params
from extensions import limiter

stats_bp = Blueprint("stats", __name__)

VALID_GROUP_BY = ("asset", "category")


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
    """Inverse of most-borrowed — surfaces idle stock, including assets
    with zero transactions ever. Uses a LEFT JOIN so those still show up;
    the date filter has to live in the JOIN condition rather than WHERE,
    or it would silently drop every zero-transaction asset.

    total_count here is just every non-deleted asset — the LEFT JOIN
    guarantees exactly one grouped row per asset regardless of the date
    filter, so no subquery is needed to get the same number."""
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
            GROUP BY {group_clause}
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
    """Counts how many times each member has gone overdue — a transaction
    counts if it was returned late, or is still open and past due right
    now. A pattern signal (repeat lateness), distinct from
    members.is_restricted, which only reflects overdue status *right now*.

    Returns both overdue_count (all matching transactions, the original
    definition) and still_open_count (how many of those are ongoing,
    unresolved loans) so it's clear how much of the number reflects
    in-progress lateness vs. actual past late-returns — rather than
    quietly picking one definition."""
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
    """How often assets/categories come back *worse* than they went out —
    a damage-rate signal. Condition states are ranked by severity
    (functional < for_repair < inoperable < disposed) since SQLite has no
    built-in notion of order among them; a transaction only counts as
    'worsened' if condition_in ranks higher (worse) than condition_out.
    Only counts transactions recorded after condition_in existed (older
    'returned' rows have condition_in IS NULL and are excluded)."""
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

    rank_case = """
        CASE %s
            WHEN 'functional' THEN 1
            WHEN 'for_repair' THEN 2
            WHEN 'inoperable' THEN 3
            WHEN 'disposed' THEN 4
        END
    """
    out_rank = rank_case % "transactions.condition_out"
    in_rank = rank_case % "transactions.condition_in"

    conn = get_db_connection()

    total_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM (
            SELECT {count_group_col} AS grp
            FROM transactions
            JOIN assets ON transactions.asset_id = assets.id
            LEFT JOIN categories ON assets.category_id = categories.id
            {where_sql}
            GROUP BY {group_clause}
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
