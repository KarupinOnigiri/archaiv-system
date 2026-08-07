from flask import Blueprint, jsonify, g, request, render_template, redirect
from database import get_db_connection, get_json_or_error
from services.auth_service import login_required
from services.notification_service import PREF_FILTER_SQL, pref_filter_params
from utils.validators import parse_pagination_params, validate_date_string

notifications_bp = Blueprint("notifications", __name__)

# Shared by every query in this file that reads the notifications table --
# a row is visible to `staff` if it's the original broadcast kind (both
# recipient columns NULL: loan due/overdue alerts, meant for everyone) or
# targeted at this staff member specifically or their role. See
# 0021_notification_recipients.sql.
_VISIBILITY_SQL = (
    "(recipient_staff_id IS NULL AND recipient_role IS NULL)"
    " OR recipient_staff_id = ? OR recipient_role = ?"
)

# Read/cleared state is per-(notification, staff) -- see
# 0027_notification_per_user_read_state.sql -- not a column on the
# notifications row itself, which would be shared by everyone who can see
# a broadcast row. A missing notification_reads row means the default,
# untouched state: unread, not cleared.
_UPSERT_READ_SQL = """
    INSERT INTO notification_reads (notification_id, staff_id, is_read, read_at)
    VALUES (?, ?, 1, datetime('now', 'localtime'))
    ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_read = 1, read_at = excluded.read_at
"""
_UPSERT_CLEAR_SQL = """
    INSERT INTO notification_reads (notification_id, staff_id, is_cleared, cleared_at)
    VALUES (?, ?, 1, datetime('now', 'localtime'))
    ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_cleared = 1, cleared_at = excluded.cleared_at
"""


def get_notifications_table_context(args, staff):
    """Shared query logic for the notifications list -- used by both the
    full /notifications page (routes/pages.py) and the htmx partial
    below, the same get_*_table_context pattern used by assets/members/
    staff/transactions. Returns (context_dict, error).

    is_read/is_cleared come from a LEFT JOIN against notification_reads
    scoped to this one staff member, same as get_notification_bell_context
    in notification_service.py -- explicit column list (not
    notifications.*) so those two computed aliases don't collide with the
    old, no-longer-authoritative columns of the same name still sitting
    on the notifications table."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    # Parenthesized -- this is an OR-expression joining a list that's about
    # to be glued together with " AND ", and SQL's AND binds tighter than
    # OR, so an unparenthesized entry here would silently let every filter
    # after it apply only to the last disjunct instead of the whole
    # visibility check.
    where_clauses = [f"({_VISIBILITY_SQL})"]
    params = [staff["id"], staff["role"]]
    # Drops any type this staff member has muted in Settings & privacy --
    # see PREF_FILTER_SQL's docstring in notification_service.py.
    where_clauses.append(PREF_FILTER_SQL.removeprefix("AND "))
    params.extend(pref_filter_params(staff))
    # A cleared notification is gone from this list for good (until
    # cleared again isn't a thing -- there's no "show cleared" view here),
    # same as the bell dropdown -- see 0025_notification_clearing.sql.
    where_clauses.append("COALESCE(nr.is_cleared, 0) = 0")

    show_unread_only = args.get("show_unread_only") == "1"
    if show_unread_only:
        where_clauses.append("COALESCE(nr.is_read, 0) = 0")

    # Mirrors routes/staff.py's get_audit_log_context: filters on the
    # date portion of created_at, inclusive on both ends.
    since = args.get("since")
    if since:
        error = validate_date_string(since, "since")
        if error:
            return None, error
        where_clauses.append("date(notifications.created_at) >= ?")
        params.append(since)

    until = args.get("until")
    if until:
        error = validate_date_string(until, "until")
        if error:
            return None, error
        where_clauses.append("date(notifications.created_at) <= ?")
        params.append(until)

    where_sql = f"WHERE {' AND '.join(where_clauses)}"
    join_sql = "LEFT JOIN notification_reads nr ON nr.notification_id = notifications.id AND nr.staff_id = ?"
    join_params = [staff["id"]]

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM notifications {join_sql} {where_sql}",
        (*join_params, *params),
    ).fetchone()["cnt"]
    notifications = conn.execute(
        f"""
        SELECT notifications.id, notifications.type, notifications.transaction_id,
               notifications.unrestriction_request_id, notifications.recipient_staff_id,
               notifications.recipient_role, notifications.message, notifications.created_at,
               COALESCE(nr.is_read, 0) AS is_read,
               COALESCE(nr.is_cleared, 0) AS is_cleared
        FROM notifications
        {join_sql}
        {where_sql}
        ORDER BY notifications.created_at DESC, notifications.id DESC
        LIMIT ? OFFSET ?
        """,
        (*join_params, *params, limit, offset),
    ).fetchall()
    conn.close()

    return {
        "notifications": notifications,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_unread_only": show_unread_only,
        "since": since,
        "until": until,
    }, None


@notifications_bp.route("/notifications/table")
@login_required
def view_notifications_table():
    """htmx partial -- returns just the list + pagination footer, so
    filtering/paging/sorting never re-renders the whole page (matching
    the assets/members/staff/transactions pattern)."""
    context, error = get_notifications_table_context(request.args, g.staff)
    if error:
        return error
    return render_template("notifications/_table.html", **context)


@notifications_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    """Scoped to what this staff member can actually see -- without the
    visibility filter, a tool_keeper clicking this would also silently
    mark every admin-only notification read for themselves. Writes one
    notification_reads row per matching notification for THIS staff_id
    only (INSERT...SELECT...ON CONFLICT) -- never touches any other
    staff member's copy of a broadcast notification, which is the whole
    point of 0027_notification_per_user_read_state.sql."""
    conn = get_db_connection()
    conn.execute(
        f"""
        INSERT INTO notification_reads (notification_id, staff_id, is_read, read_at)
        SELECT notifications.id, ?, 1, datetime('now', 'localtime')
        FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_read, 0) = 0
          AND COALESCE(nr.is_cleared, 0) = 0
          AND ({_VISIBILITY_SQL})
          {PREF_FILTER_SQL}
        ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_read = 1, read_at = excluded.read_at
        """,
        (g.staff["id"], g.staff["id"], g.staff["id"], g.staff["role"], *pref_filter_params(g.staff)),
    )
    conn.commit()
    conn.close()
    return jsonify({"message": "All notifications marked as read"})


@notifications_bp.route("/notifications/<int:id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(id):
    conn = get_db_connection()
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute(_UPSERT_READ_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification marked as read"})


# # # CLEARING ---------------------------------------------------------------
# Notifications no longer disappear on their own once the underlying loan
# is resolved (see 0025_notification_clearing.sql and
# services/notification_service.py's sync_loan_notifications) -- clearing
# here is now the only way one leaves the bell dropdown or the full
# /notifications list, and it's per staff member (0027_notification_
# per_user_read_state.sql): one person clearing a broadcast notification
# never removes it from anyone else's list.


@notifications_bp.route("/notifications/<int:id>/clear", methods=["POST"])
@login_required
def clear_notification(id):
    """Single-notification dismiss -- the small '×' on each row in both
    the bell dropdown and the full list, for clearing one at a time
    without going through the multi-select flow below."""
    conn = get_db_connection()
    notification = conn.execute(
        f"SELECT id FROM notifications WHERE id = ? AND ({_VISIBILITY_SQL})",
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute(_UPSERT_CLEAR_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification cleared"})


@notifications_bp.route("/notifications/clear", methods=["POST"])
@login_required
def clear_notifications():
    """Multi-select bulk dismiss -- the "Clear selected" action on the
    full /notifications page, driven by the row checkboxes there. Body:
    {"ids": [1, 2, 3]}. Scoped by the same visibility check as every
    other notification action here, so a staff member can't clear rows
    that were never theirs to see -- ids outside that set are silently
    skipped rather than erroring, same spirit as the bulk-deactivate
    "skipped" pattern in routes/staff.py, since a stale checkbox (another
    tab already cleared it) isn't something worth failing the whole
    request over."""
    data, error = get_json_or_error()
    if error:
        return error

    ids = data.get("ids")
    if not isinstance(ids, list) or not ids:
        return jsonify({"error": "ids must be a non-empty list"}), 400
    if not all(isinstance(i, int) for i in ids):
        return jsonify({"error": "ids must be a list of integers"}), 400

    conn = get_db_connection()
    placeholders = ",".join("?" * len(ids))
    cursor = conn.execute(
        f"""
        INSERT INTO notification_reads (notification_id, staff_id, is_cleared, cleared_at)
        SELECT notifications.id, ?, 1, datetime('now', 'localtime')
        FROM notifications
        WHERE notifications.id IN ({placeholders}) AND ({_VISIBILITY_SQL})
        ON CONFLICT(notification_id, staff_id) DO UPDATE SET is_cleared = 1, cleared_at = excluded.cleared_at
        """,
        (g.staff["id"], *ids, g.staff["id"], g.staff["role"]),
    )
    cleared_count = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({"message": f"{cleared_count} notification(s) cleared", "cleared_count": cleared_count})


@notifications_bp.route("/notifications/<int:id>/open")
@login_required
def open_notification(id):
    """The click target for a notification, from both the bell dropdown
    and the full /notifications page: marks it read, then redirects
    somewhere useful depending on what kind of notification this is.

    Loan due/overdue alerts (transaction_id set) go to the Borrow & Return
    tab filtered to that borrower's loans (?member_id=<id>, an exact-match
    filter -- see _build_transactions_filters in routes/transactions.py)
    with ?open_transaction=<id> on top, which auto-opens that one loan's
    existing details modal. So the destination shows every loan this
    borrower currently has out (useful context -- an overdue member often
    has more than one item outstanding) with the specific one the
    notification was about immediately front-and-center in its modal.
    Works even if that loan was returned moments ago and the scheduled
    sync hasn't cleared this notification yet, since both the filter and
    the details modal look it up regardless of current status.

    Unrestriction-request notifications (unrestriction_request_id set)
    just go to Approvals -- there's no equivalent single-item deep link
    there, the request is easy enough to find in a queue that's usually
    short.

    Deliberately a GET (so it can be a plain <a href>, working identically
    in both places without duplicated JS) even though it has the side
    effect of marking the row read (for this staff member only -- see
    0027_notification_per_user_read_state.sql) -- a deviation from strict
    REST, but the standard, low-stakes pattern for "click a notification,
    it opens and clears" (email clients do the same)."""
    conn = get_db_connection()
    notification = conn.execute(
        f"""
        SELECT notifications.id, notifications.unrestriction_request_id,
               notifications.transaction_id, transactions.member_id
        FROM notifications
        LEFT JOIN transactions ON transactions.id = notifications.transaction_id
        WHERE notifications.id = ? AND ({_VISIBILITY_SQL})
        """,
        (id, g.staff["id"], g.staff["role"]),
    ).fetchone()
    if notification is None:
        conn.close()
        return redirect("/notifications")

    conn.execute(_UPSERT_READ_SQL, (id, g.staff["id"]))
    conn.commit()
    conn.close()

    if notification["unrestriction_request_id"] is not None:
        return redirect("/requests")

    # Falls through to /notifications (rather than erroring) for any row
    # that's neither kind -- shouldn't happen given the type CHECK
    # constraint, but a dead-end redirect beats a 500 if it ever does.
    if notification["transaction_id"] is not None:
        return redirect(
            f"/transactions?member_id={notification['member_id']}"
            f"&open_transaction={notification['transaction_id']}"
        )

    return redirect("/notifications")
