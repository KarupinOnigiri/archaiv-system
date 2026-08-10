from database import get_db_connection
import logging

logger = logging.getLogger(__name__)


def log_action(
    staff_id, action, target_table=None, target_id=None, details=None, target_name=None
):
    """target_name is a snapshot of the target's label at the time of the action, not something
    re-joined later (which would go blank for deleted/renamed/cleaned-up rows). Pass it whenever
    you have it in scope; the Activity Log falls back to "table #id" when it's None."""
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
