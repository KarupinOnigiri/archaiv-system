from database import get_db_connection
from utils.logger import log_action
from utils.formatters import full_name
from utils.constants import SESSION_STALE_MINUTES
import logging

logger = logging.getLogger(__name__)


def cleanup_stale_checkout_sessions():
    """Releases reserved assets and cancels checkout sessions abandoned past the stale timeout."""
    conn = get_db_connection()

    stale_sessions = conn.execute(
        """
        SELECT checkout_sessions.id, members.first_name, members.last_name,
               members.middle_name, members.suffix
        FROM checkout_sessions
        JOIN members ON members.id = checkout_sessions.member_id
        WHERE checkout_sessions.status = 'open'
        AND checkout_sessions.created_at < datetime('now', 'localtime', ?)
        """,
        (f"-{SESSION_STALE_MINUTES} minutes",),
    ).fetchall()

    for session in stale_sessions:
        session_id = session["id"]
        session_member_name = full_name(
            session["first_name"], session["last_name"], session["middle_name"], session["suffix"]
        )
        try:
            conn.execute(
                """
                UPDATE assets SET status = 'available'
                WHERE id IN (SELECT asset_id FROM checkout_session_items WHERE checkout_session_id = ?)
                AND status = 'reserved'
                """,
                (session_id,),
            )
            conn.execute(
                "DELETE FROM checkout_session_items WHERE checkout_session_id = ?",
                (session_id,),
            )
            conn.execute(
                "UPDATE checkout_sessions SET status = 'cancelled', completed_at = datetime('now', 'localtime') WHERE id = ?",
                (session_id,),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.warning(f"Failed to auto-cancel stale session {session_id}: {e}")
            continue

        log_action(
            staff_id=None,
            action="auto_cancelled_checkout_session",
            target_table="checkout_sessions",
            target_id=session_id,
            target_name=session_member_name,
            details=f"System auto-cancelled abandoned checkout session {session_id} for {session_member_name} after {SESSION_STALE_MINUTES} minutes of inactivity",
        )

    conn.close()
