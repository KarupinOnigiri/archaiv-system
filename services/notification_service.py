from database import get_db_connection
from utils.formatters import full_name
import logging

logger = logging.getLogger(__name__)

# A "due soon" alert fires once a borrowed item's due date is within this
# many hours -- long enough for staff to catch it on the next scheduled
# sync (every 15 min, see app.py) well before it actually tips overdue.
DUE_SOON_HOURS = 24


def _stale_notification_ids(conn):
    """Notification rows that no longer describe reality: the transaction
    was returned, or (via renewal) its due_date moved back out of the
    window that earned it a 'due_soon' or 'overdue' row in the first
    place. Collected up front and deleted before the insert pass below,
    so a renewed loan's stale "overdue" banner doesn't linger next to a
    fresh, accurate one."""
    rows = conn.execute(
        """
        SELECT notifications.id
        FROM notifications
        JOIN transactions ON transactions.id = notifications.transaction_id
        WHERE transactions.status != 'borrowed'
           OR transactions.due_date IS NULL
           OR (
                notifications.type = 'overdue'
                AND transactions.due_date >= datetime('now', 'localtime')
              )
           OR (
                notifications.type = 'due_soon'
                AND (
                    transactions.due_date < datetime('now', 'localtime')
                    OR transactions.due_date > datetime('now', 'localtime', ?)
                )
              )
        """,
        (f"+{DUE_SOON_HOURS} hours",),
    ).fetchall()
    return [row["id"] for row in rows]


def sync_loan_notifications():
    """Reconciles the notifications table against the current state of
    open loans: clears alerts that no longer apply (returned/renewed),
    then creates one 'due_soon' or 'overdue' row per qualifying open loan
    that doesn't already have one. Meant to run on a schedule (see
    app.py, alongside sweep_overdue_members) -- opens its own connection
    for that reason, same pattern as the other scheduled jobs."""
    conn = get_db_connection()
    try:
        stale_ids = _stale_notification_ids(conn)
        if stale_ids:
            conn.executemany(
                "DELETE FROM notifications WHERE id = ?", [(i,) for i in stale_ids]
            )

        candidates = conn.execute(
            """
            SELECT transactions.id AS transaction_id, transactions.due_date,
                   assets.name AS asset_name,
                   members.first_name, members.last_name, members.middle_name, members.suffix,
                   CASE
                       WHEN transactions.due_date < datetime('now', 'localtime') THEN 'overdue'
                       ELSE 'due_soon'
                   END AS calc_type
            FROM transactions
            JOIN assets ON assets.id = transactions.asset_id
            JOIN members ON members.id = transactions.member_id
            WHERE transactions.status = 'borrowed'
              AND transactions.due_date IS NOT NULL
              AND transactions.due_date <= datetime('now', 'localtime', ?)
            """,
            (f"+{DUE_SOON_HOURS} hours",),
        ).fetchall()

        created = 0
        for row in candidates:
            member_name = full_name(
                row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
            )
            if row["calc_type"] == "overdue":
                message = f"{row['asset_name']} borrowed by {member_name} is overdue"
            else:
                message = f"{row['asset_name']} borrowed by {member_name} is due soon"

            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO notifications (type, transaction_id, message)
                VALUES (?, ?, ?)
                """,
                (row["calc_type"], row["transaction_id"], message),
            )
            if cursor.rowcount:
                created += 1

        conn.commit()
        if stale_ids or created:
            logger.info(
                f"Synced loan notifications: {created} created, {len(stale_ids)} cleared as stale"
            )
    except Exception:
        conn.rollback()
        logger.exception("Failed to sync loan notifications")
    finally:
        conn.close()


def get_notification_bell_context(conn, limit=8):
    """Returns (notifications, unread_count) for the sidebar bell dropdown
    -- most recent first, capped at `limit` rows so the popup stays short.
    Used by app.py's context processor on every page load."""
    notifications = conn.execute(
        """
        SELECT * FROM notifications
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    unread_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM notifications WHERE is_read = 0"
    ).fetchone()["cnt"]
    return notifications, unread_count
