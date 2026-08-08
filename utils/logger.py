from database import get_db_connection
import logging

logger = logging.getLogger(__name__)


def log_action(
    staff_id, action, target_table=None, target_id=None, details=None, target_name=None
):
    """target_name is a snapshot of the target's human-readable label at
    the moment this action happened (an asset's name, a member's full
    name, a staff username, ...) -- not something resolved later by
    joining back to the live row, which would go blank for anything
    since deleted/archived/renamed, or for rows (checkout sessions,
    unrestriction requests, ...) that get cleaned up once resolved.
    Every call site that already has the target's name in scope (which
    is nearly all of them -- they just created/fetched/modified it)
    should pass it here; the Activity Log's Target column falls back to
    the bare "table #id" form when it's None."""
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO audit_log (staff_id, action, target_table, target_id, details, target_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (staff_id, action, target_table, target_id, details, target_name),
        )
        conn.commit()
    except Exception as e:
        logger.warning(
            f"Audit log write failed (action='{action}', "
            f"target_table='{target_table}', target_id={target_id}): {e}"
        )
    finally:
        if conn is not None:
            conn.close()
