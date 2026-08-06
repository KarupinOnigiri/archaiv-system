from flask import Blueprint, jsonify, request, render_template, redirect
from database import get_db_connection
from services.auth_service import login_required
from utils.validators import parse_pagination_params

notifications_bp = Blueprint("notifications", __name__)


def get_notifications_table_context(args):
    """Shared query logic for the notifications list -- used by both the
    full /notifications page (routes/pages.py) and, if ever needed, an
    htmx partial refetch. Mirrors the get_*_table_context pattern used by
    assets/members/staff/transactions. Returns (context_dict, error)."""
    limit, offset, error = parse_pagination_params(args)
    if error:
        return None, error

    show_unread_only = args.get("show_unread_only") == "1"
    where_sql = "WHERE is_read = 0" if show_unread_only else ""

    conn = get_db_connection()
    total_count = conn.execute(
        f"SELECT COUNT(*) AS cnt FROM notifications {where_sql}"
    ).fetchone()["cnt"]
    notifications = conn.execute(
        f"""
        SELECT * FROM notifications
        {where_sql}
        ORDER BY created_at DESC, id DESC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    ).fetchall()
    conn.close()

    return {
        "notifications": notifications,
        "total_count": total_count,
        "limit": limit,
        "offset": offset,
        "show_unread_only": show_unread_only,
    }, None


@notifications_bp.route("/notifications/mark-all-read", methods=["POST"])
@login_required
def mark_all_notifications_read():
    conn = get_db_connection()
    conn.execute("UPDATE notifications SET is_read = 1 WHERE is_read = 0")
    conn.commit()
    conn.close()
    return jsonify({"message": "All notifications marked as read"})


@notifications_bp.route("/notifications/<int:id>/mark-read", methods=["POST"])
@login_required
def mark_notification_read(id):
    conn = get_db_connection()
    notification = conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (id,)
    ).fetchone()
    if notification is None:
        conn.close()
        return jsonify({"error": "Notification not found"}), 404

    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return jsonify({"message": "Notification marked as read"})


@notifications_bp.route("/notifications/<int:id>/open")
@login_required
def open_notification(id):
    """The click target for a notification, from both the bell dropdown
    and the full /notifications page: marks it read, then redirects to
    the exact loan it's about on the Borrow & Return tab (via the
    transaction_id filter added to get_transactions_table_context) --
    that filter surfaces the row regardless of the loan's current status
    or whatever other filters/pagination happen to be active, so this
    still works even if the loan was returned moments ago and the
    scheduled sync hasn't cleared this notification yet.

    Deliberately a GET (so it can be a plain <a href>, working identically
    in both places without duplicated JS) even though it has the side
    effect of marking the row read -- a deviation from strict REST, but
    the standard, low-stakes pattern for "click a notification, it opens
    and clears" (email clients do the same)."""
    conn = get_db_connection()
    notification = conn.execute(
        "SELECT * FROM notifications WHERE id = ?", (id,)
    ).fetchone()
    if notification is None:
        conn.close()
        return redirect("/notifications")

    conn.execute("UPDATE notifications SET is_read = 1 WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    return redirect(f"/transactions?transaction_id={notification['transaction_id']}")
