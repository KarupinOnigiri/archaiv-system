from database import get_db_connection
from utils.formatters import full_name
from services.settings_service import get_due_soon_hours, is_feature_enabled
import logging

logger = logging.getLogger(__name__)


def sync_loan_notifications():
    """Creates one 'due_soon' or 'overdue' row per qualifying open loan
    that doesn't already have one. Meant to run on a schedule (see
    app.py, alongside sweep_overdue_members) -- opens its own connection
    for that reason, same pattern as the other scheduled jobs.

    Deliberately does NOT delete or touch notifications for loans that
    have since been returned or renewed out of the due-soon window --
    see 0025_notification_clearing.sql. A notification now persists
    exactly as-is (read or unread) until a staff member explicitly clears
    it via routes/notifications.py's clear endpoints, the same way a
    Facebook notification doesn't disappear just because its underlying
    post got deleted. The (transaction_id, type) UNIQUE constraint still
    does its job here: a loan that's still due-soon (or still overdue) on
    the next run just no-ops via INSERT OR IGNORE rather than creating a
    duplicate, cleared or not."""
    conn = get_db_connection()
    try:
        if not is_feature_enabled("transactions", conn):
            # Borrow & Return is disabled -- no point generating due/overdue
            # alerts for a workflow an admin has turned off (see
            # migrations/0033_feature_flags.sql).
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
                # Escalate an existing due_soon row in place rather than
                # leaving it as a second, stale row alongside a fresh
                # 'overdue' one for the same loan -- since notifications
                # no longer get cleaned up automatically, that pair would
                # otherwise sit side by side forever. Also wipes every
                # staff member's per-user read/cleared state for this row
                # (see 0027_notification_per_user_read_state.sql), not
                # just the requesting user's -- crossing into overdue is
                # a genuinely more urgent state, worth resurfacing for
                # EVERYONE who can see it, even those who'd already read
                # or cleared the due-soon version.
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


# Appended to any notifications query that should honor the per-staff
# toggles set in the "Settings & privacy" panel (templates/base.html,
# routes/staff.py's notification-prefs endpoints -- see
# 0022_staff_notification_prefs.sql). A muted type is filtered out
# everywhere a staff member would *see* their own notification list (bell
# dropdown, full /notifications page, unread count) -- but note this is a
# personal display preference, not a permission: it doesn't affect who a
# notification is generated for or who else can see it.
PREF_FILTER_SQL = (
    "AND (type NOT IN ('due_soon') OR ? = 1)"
    " AND (type NOT IN ('overdue') OR ? = 1)"
    " AND (type NOT IN ('unrestriction_requested', 'unrestriction_resolved') OR ? = 1)"
)


def pref_filter_params(staff):
    """Param tuple matching PREF_FILTER_SQL's three placeholders, pulled
    from any row (g.staff, or app.py's staff_member) that selected the
    notify_due_soon/notify_overdue/notify_requests columns."""
    return (
        staff["notify_due_soon"],
        staff["notify_overdue"],
        staff["notify_requests"],
    )


def get_notification_bell_context(conn, staff_id, role, staff, limit=8):
    """Returns (notifications, unread_count) for the sidebar bell dropdown
    -- most recent first, capped at `limit` rows so the popup stays short.
    Used by app.py's context processor on every page load. `staff` here
    only needs to carry the three notify_* pref columns (see
    pref_filter_params) -- kept separate from staff_id/role rather than
    folded into one row argument since callers already have those two on
    hand from other queries (e.g. app.py's staff_member, which selects a
    handful of columns, not `SELECT staff.*`).

    Scoped to what this staff_id/role can actually see: a row with both
    recipient columns NULL is the original broadcast design (loan due/
    overdue alerts, meant for everyone), while a row with recipient_
    staff_id or recipient_role set is only visible to that specific
    person or role -- see 0021_notification_recipients.sql. On top of
    that, PREF_FILTER_SQL drops any type this staff member has muted.

    is_read/is_cleared come from a LEFT JOIN against notification_reads,
    scoped to this one staff_id (see 0027_notification_per_user_read_
    state.sql) -- NOT from columns on the notifications row itself, which
    would be shared by everyone who can see a broadcast row. Explicit
    column list (not notifications.*) so those two computed aliases
    don't collide with the old, no-longer-authoritative columns of the
    same name still sitting on the table."""
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
# Unlike the loan due/overdue sync above (a periodic reconciliation job),
# these fire once, imperatively, at the exact point the triggering action
# happens in routes/requests.py -- request creation and resolution are
# discrete one-time events, not continuously-drifting state, so there's
# nothing to periodically reconcile the way loan due dates need.


def create_unrestriction_requested_notifications(conn, request_id, member_name, requester_username):
    """Notifies every admin and in_charge that a tool_keeper filed an
    unrestriction request -- one row per role (not one broadcast row),
    so each role's read state is independent and mark_all_notifications_
    read scoped to one role never touches the other's copy."""
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


# # # FEEDBACK LIFECYCLE ----------------------------------------------------
# Same one-shot-imperative shape as the unrestriction functions above --
# fired once from routes/feedback.py at the exact point each event
# happens (submit / resolve), not a periodic reconciliation job.


def create_feedback_submitted_notifications(conn, report_id, submitter_staff_id, submitter_name, category):
    """Two rows per submission: a broadcast to every admin so the report
    actually lands somewhere they'll see it (not just the /feedback tab
    they'd have to remember to check), and a personal confirmation back
    to whoever filed it -- so hitting 'Submit' doesn't feel like shouting
    into a void, same reason a real support-ticket form emails you a
    receipt. Sent to the submitter regardless of role (including an admin
    reporting their own bug) -- simpler than special-casing which roles
    get confirmed, and there's no downside to an admin also getting one."""
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
    """Removes the admin/in_charge 'pending review' notices for a request
    that's just been approved, denied, or cancelled -- there's nothing
    left for them to review, so the notice shouldn't keep sitting in
    their list implying otherwise. Called before the resolved-notice
    insert above (approve/deny) or on its own (cancel, which has no
    resolved-notice counterpart)."""
    conn.execute(
        "DELETE FROM notifications WHERE unrestriction_request_id = ? AND type = 'unrestriction_requested'",
        (request_id,),
    )
