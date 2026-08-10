from database import get_db_connection
from utils.formatters import full_name
from services.settings_service import get_due_soon_hours, is_feature_enabled
import logging

logger = logging.getLogger(__name__)


def sync_loan_notifications():
    """Creates one 'due_soon' or 'overdue' row per qualifying open loan that
    doesn't already have one. Runs on a schedule (see app.py), so it opens
    its own connection like the other scheduled jobs.

    Doesn't touch notifications for loans that got returned/renewed since --
    they persist until a staff member explicitly clears them (see
    0025_notification_clearing.sql), like a notification that outlives its
    source post. (transaction_id, type) UNIQUE + INSERT OR IGNORE keeps a
    still-due loan from generating duplicate rows on each run."""
    conn = get_db_connection()
    try:
        if not is_feature_enabled("transactions", conn):
            # Borrow & Return is off, so don't generate alerts for it
            return

        due_soon_hours = get_due_soon_hours(conn)

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
            (f"+{due_soon_hours} hours",),
        ).fetchall()

        created = 0
        escalated = 0
        for row in candidates:
            member_name = full_name(
                row["first_name"], row["last_name"], row["middle_name"], row["suffix"]
            )
            if row["calc_type"] == "overdue":
                message = f"{row['asset_name']} borrowed by {member_name} is overdue"
            else:
                message = f"{row['asset_name']} borrowed by {member_name} is due soon"

            if row["calc_type"] == "overdue":
                # escalate the existing due_soon row in place instead of leaving a
                # stale duplicate; also clears everyone's read/cleared state on it
                # since going overdue is worth resurfacing even if they'd read it
                existing = conn.execute(
                    "SELECT id FROM notifications WHERE transaction_id = ? AND type = 'due_soon'",
                    (row["transaction_id"],),
                ).fetchone()
                if existing:
                    conn.execute(
                        "UPDATE notifications SET type = 'overdue', message = ? WHERE id = ?",
                        (message, existing["id"]),
                    )
                    conn.execute(
                        "DELETE FROM notification_reads WHERE notification_id = ?",
                        (existing["id"],),
                    )
                    escalated += 1
                    continue

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
        if created or escalated:
            logger.info(
                f"Synced loan notifications: {created} created, {escalated} escalated to overdue"
            )
    except Exception:
        conn.rollback()
        logger.exception("Failed to sync loan notifications")
    finally:
        conn.close()


# Append to any notifications query that should respect each staff member's
# mute toggles from "Settings & privacy". This is a display preference only --
# it hides muted types from that person's view, not who the notification
# gets created for or who else can see it.
PREF_FILTER_SQL = (
    "AND (type NOT IN ('due_soon') OR ? = 1)"
    " AND (type NOT IN ('overdue') OR ? = 1)"
    " AND (type NOT IN ('unrestriction_requested', 'unrestriction_resolved') OR ? = 1)"
)


def pref_filter_params(staff):
    """Param tuple for PREF_FILTER_SQL's three placeholders, from any row
    that selected the notify_due_soon/notify_overdue/notify_requests columns."""
    return (
        staff["notify_due_soon"],
        staff["notify_overdue"],
        staff["notify_requests"],
    )


def get_notification_bell_context(conn, staff_id, role, staff, limit=8):
    """Returns (notifications, unread_count) for the sidebar bell dropdown,
    most recent first, capped at `limit`. `staff` just needs to carry the
    three notify_* pref columns; kept separate from staff_id/role since
    callers usually already have those from elsewhere.

    Visibility: rows with both recipient columns NULL are broadcasts (loan
    due/overdue alerts), rows with recipient_staff_id/recipient_role set are
    scoped to that person/role. PREF_FILTER_SQL then drops muted types.

    is_read/is_cleared come from a LEFT JOIN on notification_reads scoped to
    this staff_id, not from columns on notifications itself (those would be
    shared across everyone seeing a broadcast row) -- explicit column list
    avoids colliding with the old same-named columns still on the table."""
    pref_params = pref_filter_params(staff)
    notifications = conn.execute(
        f"""
        SELECT notifications.id, notifications.type, notifications.transaction_id,
               notifications.unrestriction_request_id, notifications.recipient_staff_id,
               notifications.recipient_role, notifications.message, notifications.created_at,
               COALESCE(nr.is_read, 0) AS is_read,
               COALESCE(nr.is_cleared, 0) AS is_cleared
        FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_cleared, 0) = 0
          AND (
            (recipient_staff_id IS NULL AND recipient_role IS NULL)
            OR recipient_staff_id = ?
            OR recipient_role = ?
          )
        {PREF_FILTER_SQL}
        ORDER BY notifications.created_at DESC, notifications.id DESC
        LIMIT ?
        """,
        (staff_id, staff_id, role, *pref_params, limit),
    ).fetchall()
    unread_count = conn.execute(
        f"""
        SELECT COUNT(*) AS cnt FROM notifications
        LEFT JOIN notification_reads nr
          ON nr.notification_id = notifications.id AND nr.staff_id = ?
        WHERE COALESCE(nr.is_read, 0) = 0
          AND COALESCE(nr.is_cleared, 0) = 0
          AND (
            (recipient_staff_id IS NULL AND recipient_role IS NULL)
            OR recipient_staff_id = ?
            OR recipient_role = ?
          )
        {PREF_FILTER_SQL}
        """,
        (staff_id, staff_id, role, *pref_params),
    ).fetchone()["cnt"]
    return notifications, unread_count


# # # UNRESTRICTION REQUEST LIFECYCLE --------------------------------------
# Unlike the periodic sync above, these fire once, right when the triggering
# action happens in routes/requests.py -- nothing to reconcile later.


def create_unrestriction_requested_notifications(conn, request_id, member_name, requester_username):
    """Notifies every admin and in_charge that a tool_keeper filed an
    unrestriction request -- one row per role so each role's read state
    stays independent."""
    message = f"{requester_username} requested unrestriction for {member_name}"
    for role in ("admin", "in_charge"):
        conn.execute(
            """
            INSERT INTO notifications (type, unrestriction_request_id, recipient_role, message)
            VALUES ('unrestriction_requested', ?, ?, ?)
            """,
            (request_id, role, message),
        )


def create_unrestriction_resolved_notification(conn, request_id, requester_staff_id, member_name, approved, review_notes=None):
    """Notifies the original requester once their request is approved or
    denied -- targeted at that one staff member, not broadcast. Not
    called on cancellation (the requester cancelled it themselves, so
    they already know; see clear_unrestriction_pending_notifications)."""
    if approved:
        message = f"Your unrestriction request for {member_name} was approved"
    else:
        message = f"Your unrestriction request for {member_name} was denied"
        if review_notes:
            message += f": {review_notes}"
    conn.execute(
        """
        INSERT INTO notifications (type, unrestriction_request_id, recipient_staff_id, message)
        VALUES ('unrestriction_resolved', ?, ?, ?)
        """,
        (request_id, requester_staff_id, message),
    )


# # # ADMIN DEACTIVATION REQUEST LIFECYCLE -----------------------------------
# Same one-shot-imperative shape as the unrestriction functions above --
# fired once from routes/staff.py right when each event happens.


def create_admin_deactivation_requested_notifications(conn, request_id, target_name, requester_name):
    """Notifies every in_charge/tool_keeper account -- anyone eligible to
    review this request, since all of them can vote on any request now
    regardless of tier. One row per role. Used to notify nobody; reviewers
    only found out by checking the Approvals tab themselves.

    target_name/requester_name are full names, not usernames -- matches
    how the deactivation table itself displays people."""
    message = f"{requester_name} requested deactivation of admin {target_name}"
    for role in ("in_charge", "tool_keeper"):
        conn.execute(
            """
            INSERT INTO notifications (type, admin_deactivation_request_id, recipient_role, message)
            VALUES ('admin_deactivation_requested', ?, ?, ?)
            """,
            (request_id, role, message),
        )


def create_admin_deactivation_resolved_notification(conn, request_id, requester_staff_id, target_name, approved, denial_reason=None):
    """Notifies the original requester once their admin-deactivation
    request is approved or denied -- targeted at that one staff member,
    not broadcast. Not called on the requester's own cancellation (they
    already know). target_name is a full name, not a username."""
    if approved:
        message = f"Your request to deactivate admin {target_name} was approved"
    else:
        message = f"Your request to deactivate admin {target_name} was denied"
        if denial_reason:
            message += f": {denial_reason}"
    conn.execute(
        """
        INSERT INTO notifications (type, admin_deactivation_request_id, recipient_staff_id, message)
        VALUES ('admin_deactivation_resolved', ?, ?, ?)
        """,
        (request_id, requester_staff_id, message),
    )


# # # FEEDBACK LIFECYCLE ----------------------------------------------------
# Same one-shot shape as above -- fired once from routes/feedback.py right
# when each event happens (submit / resolve).


def create_feedback_submitted_notifications(conn, report_id, submitter_staff_id, submitter_name, category):
    """Two rows per submission: a broadcast to every admin, and a personal
    confirmation back to the submitter (even if they're an admin themselves)
    so submitting doesn't feel like shouting into a void."""
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_role, message)
        VALUES ('feedback_submitted', ?, 'admin', ?)
        """,
        (report_id, f"{submitter_name} submitted {category} feedback"),
    )
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_staff_id, message)
        VALUES ('feedback_submitted', ?, ?, 'Your feedback was sent to the admins.')
        """,
        (report_id, submitter_staff_id),
    )


def create_feedback_resolved_notification(conn, report_id, submitter_staff_id, response):
    """Notifies the original submitter once an admin resolves their
    report, carrying the admin's actual reply text -- not a silent status
    flip. Not sent on reopen; there's nothing new to tell them yet."""
    conn.execute(
        """
        INSERT INTO notifications (type, feedback_report_id, recipient_staff_id, message)
        VALUES ('feedback_resolved', ?, ?, ?)
        """,
        (report_id, submitter_staff_id, f"Your feedback was resolved: {response}"),
    )


def clear_unrestriction_pending_notifications(conn, request_id):
    """Removes the admin/in_charge 'pending review' notices once a request
    is approved, denied, or cancelled -- nothing left to review. Called
    before the resolved-notice insert (approve/deny) or standalone (cancel,
    which has no resolved notice)."""
    conn.execute(
        "DELETE FROM notifications WHERE unrestriction_request_id = ? AND type = 'unrestriction_requested'",
        (request_id,),
    )
