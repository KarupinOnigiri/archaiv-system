from database import get_db_connection
import logging

logger = logging.getLogger(__name__)


def log_action(
    staff_id, action, target_table=None, target_id=None, details=None, target_name=None
):
    """Mag-insert og row sa audit_log table - ang Activity Log tab dinhi
    mismo mubasa, so kada importante nga aksyon (add/edit/delete/checkout/
    login) angay mutawag ani. Mapakyas man mag-log ra, dili ni ipahunong
    ang tinuod nga aksyon.

    target_name kay snapshot sa label SA PANAHON sa aksyon (dili re-joined
    later - mahimong blangko na kung na-delete/na-rename na ang row).

    Returns: None - fire-and-forget."""
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
