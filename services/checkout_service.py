from database import get_db_connection
from utils.logger import log_action
from database import get_db_connection
from utils.constants import SESSION_STALE_MINUTES


def cleanup_stale_checkout_sessions():
    """Finds every open checkout session across all members that's gone
    stale, releases its reserved assets back to available, and marks it
    cancelled. Meant to be run on a schedule so abandoned carts don't lock
    tools indefinitely — not tied to any one member coming back to the kiosk."""
    conn = get_db_connection()

    stale_sessions = conn.execute(
        """
        SELECT id FROM checkout_sessions
        WHERE status = 'open'
        AND created_at < datetime('now', 'localtime', ?)
        """,
        (f"-{SESSION_STALE_MINUTES} minutes",),
    ).fetchall()

    for session in stale_sessions:
        session_id = session["id"]
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
            print(f"WARNING: failed to auto-cancel stale session {session_id}: {e}")
            continue

        log_action(
            staff_id=None,
            action="auto_cancelled_checkout_session",
            target_table="checkout_sessions",
            target_id=session_id,
            details=f"Auto-cancelled abandoned checkout session {session_id} after {SESSION_STALE_MINUTES} minutes of inactivity",
        )

    conn.close()
