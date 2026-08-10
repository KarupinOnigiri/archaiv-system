from database import get_db_connection
from services.settings_service import get_due_soon_hours
from utils.formatters import full_name

# Lower-bound SQL fragment for the Top Borrowed panel's range filter, keyed by the <select>
# value. "today"/"yesterday" anchor to the start of a calendar day (localtime), not a rolling 24h window.
TOP_BORROWED_RANGES = {
    "today": "datetime('now', 'localtime', 'start of day')",
    "yesterday": "datetime('now', 'localtime', 'start of day', '-1 day')",
    "7d": "datetime('now', 'localtime', '-7 days')",
    "30d": "datetime('now', 'localtime', '-30 days')",
}
# Upper bound only matters for "yesterday" (everything else's upper bound
# is just "now", i.e. no upper filter needed).
TOP_BORROWED_UPPER_BOUND = {
    "yesterday": "datetime('now', 'localtime', 'start of day')",
}

# Empty-state wording for templates/dashboard/_top_borrowed_list.html --
# "No borrows {{ range_label }}."
TOP_BORROWED_RANGE_LABELS = {
    "today": "today",
    "yesterday": "yesterday",
    "7d": "in the last 7 days",
    "30d": "in the last 30 days",
}


def get_top_borrowed(conn, range_key):
    """Top 5 most-borrowed assets in the given range -- range_key must be
    one of TOP_BORROWED_RANGES' keys (callers validate this; see
    routes/dashboard.py). Shared by get_dashboard_context's initial
    server render and the htmx-refreshed panel so both use the exact same
    query, never two competing definitions of "top borrowed"."""
    lower_bound = TOP_BORROWED_RANGES[range_key]
    upper_clause = ""
    params = []
    if range_key in TOP_BORROWED_UPPER_BOUND:
        upper_clause = f"AND transactions.time_out < {TOP_BORROWED_UPPER_BOUND[range_key]}"
    return conn.execute(
        f"""
        SELECT assets.id AS asset_id, assets.name AS asset_name,
               assets.asset_id AS asset_code, COUNT(*) AS borrow_count
        FROM transactions
        JOIN assets ON assets.id = transactions.asset_id
        WHERE transactions.time_out >= {lower_bound}
          {upper_clause}
        GROUP BY transactions.asset_id
        ORDER BY borrow_count DESC, asset_name ASC
        LIMIT 5
        """,
        params,
    ).fetchall()


def get_dashboard_context(conn=None):
    """Everything the Dashboard (templates/home.html, served by app.py's
    home()) needs in one place. Every count here mirrors the exact query
    each real tab uses for its own stat cards (assets_page/members_page/
    transactions_page in routes/pages.py) rather than inventing a new
    definition of "available"/"overdue"/etc. -- so a number on the
    dashboard can never quietly disagree with the tab it links to.

    Read-only and cheap (a handful of COUNT/LIMIT queries), fine to run
    on every dashboard load rather than caching -- same reasoning as
    every other *_page() route in this app."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()

    # # # Inventory -----------------------------------------------------
    asset_totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN status = 'available' THEN 1 ELSE 0 END) AS available,
            SUM(CASE WHEN status = 'borrowed' THEN 1 ELSE 0 END) AS borrowed,
            SUM(CASE WHEN status = 'maintenance' THEN 1 ELSE 0 END) AS maintenance,
            -- 'reserved' = held for an in-progress checkout session that
            -- hasn't been confirmed yet (services/checkout_service.py) --
            -- included so the status breakdown always accounts for every
            -- asset, not just the three tracked by the Inventory tab's own
            -- stat cards.
            SUM(CASE WHEN status = 'reserved' THEN 1 ELSE 0 END) AS reserved
        FROM assets
        WHERE deleted_at IS NULL
        """
    ).fetchone()

    # # # Members --------------------------------------------------------
    member_totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN is_active = 1 AND is_restricted = 0 THEN 1 ELSE 0 END) AS active,
            SUM(CASE WHEN is_active = 1 AND is_restricted = 1 THEN 1 ELSE 0 END) AS restricted
        FROM members
        """
    ).fetchone()

    # # # Loans -----------------------------------------------------------
    # Same "overdue" condition as transactions_page's stat_overdue.
    loan_totals = conn.execute(
        """
        SELECT
            SUM(CASE WHEN status = 'borrowed' THEN 1 ELSE 0 END) AS borrowed,
            SUM(CASE WHEN status = 'borrowed' AND due_date IS NOT NULL
                     AND due_date < datetime('now', 'localtime') THEN 1 ELSE 0 END) AS overdue
        FROM transactions
        """
    ).fetchone()

    # Due Soon list -- replaces a redundant "Notifications" panel that just repeated the
    # bell dropdown. Reads as a to-do list (soonest-first) instead. Same due_soon_hours
    # window as notification_service.py's sync job, so this can't disagree with the real alert.
    due_soon_hours = get_due_soon_hours(conn)
    due_soon_rows = conn.execute(
        """
        SELECT transactions.id AS transaction_id, transactions.due_date,
               assets.name AS asset_name, members.id AS member_id,
               members.first_name, members.last_name, members.middle_name, members.suffix
        FROM transactions
        JOIN assets ON assets.id = transactions.asset_id
        JOIN members ON members.id = transactions.member_id
        WHERE transactions.status = 'borrowed' AND transactions.due_date IS NOT NULL
          AND transactions.due_date >= datetime('now', 'localtime')
          AND transactions.due_date <= datetime('now', 'localtime', ?)
        ORDER BY transactions.due_date ASC
        LIMIT 6
        """,
        (f"+{due_soon_hours} hours",),
    ).fetchall()
    due_soon = [
        {
            "transaction_id": row["transaction_id"],
            "due_date": row["due_date"],
            "asset_name": row["asset_name"],
            "member_id": row["member_id"],
            "member_name": full_name(
                row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
            ),
        }
        for row in due_soon_rows
    ]
    due_soon_count = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE status = 'borrowed' AND due_date IS NOT NULL
          AND due_date >= datetime('now', 'localtime')
          AND due_date <= datetime('now', 'localtime', ?)
        """,
        (f"+{due_soon_hours} hours",),
    ).fetchone()["cnt"]

    # # # Recent activity --------------------------------------------------
    # One feed of both "borrowed" and "returned" events -- UNION ALL over transactions,
    # tagged by which timestamp supplied the event. Caps at 8; a glance feed, not a Transactions-tab substitute.
    recent_activity = conn.execute(
        """
        SELECT * FROM (
            SELECT transactions.id AS transaction_id, 'borrowed' AS event_type,
                   transactions.time_out AS event_time,
                   assets.name AS asset_name, assets.asset_id AS asset_code,
                   members.id AS member_id, members.first_name, members.last_name,
                   members.middle_name, members.suffix
            FROM transactions
            JOIN assets ON assets.id = transactions.asset_id
            JOIN members ON members.id = transactions.member_id

            UNION ALL

            SELECT transactions.id, 'returned',
                   transactions.time_in,
                   assets.name, assets.asset_id,
                   members.id, members.first_name, members.last_name,
                   members.middle_name, members.suffix
            FROM transactions
            JOIN assets ON assets.id = transactions.asset_id
            JOIN members ON members.id = transactions.member_id
            WHERE transactions.time_in IS NOT NULL
        )
        ORDER BY event_time DESC
        LIMIT 8
        """
    ).fetchall()
    recent_activity = [
        {
            "transaction_id": row["transaction_id"],
            "event_type": row["event_type"],
            "event_time": row["event_time"],
            "asset_name": row["asset_name"],
            "asset_code": row["asset_code"],
            "member_id": row["member_id"],
            "member_name": full_name(
                row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
            ),
        }
        for row in recent_activity
    ]

    top_borrowed = get_top_borrowed(conn, "30d")

    if owns_conn:
        conn.close()

    return {
        "dash_assets_total": asset_totals["total"] or 0,
        "dash_assets_available": asset_totals["available"] or 0,
        "dash_assets_borrowed": asset_totals["borrowed"] or 0,
        "dash_assets_maintenance": asset_totals["maintenance"] or 0,
        "dash_assets_reserved": asset_totals["reserved"] or 0,
        "dash_members_total": member_totals["total"] or 0,
        "dash_members_active": member_totals["active"] or 0,
        "dash_members_restricted": member_totals["restricted"] or 0,
        "dash_loans_borrowed": loan_totals["borrowed"] or 0,
        "dash_loans_overdue": loan_totals["overdue"] or 0,
        "dash_loans_due_soon": due_soon_count,
        "dash_due_soon": due_soon,
        "dash_recent_activity": recent_activity,
        "dash_top_borrowed": top_borrowed,
        "range_label": TOP_BORROWED_RANGE_LABELS["30d"],
    }
