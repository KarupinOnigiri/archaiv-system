from database import get_db_connection
import logging

logger = logging.getLogger(__name__)


def log_action(
    staff_id, action, target_table=None, target_id=None, details=None, target_name=None, conn=None
):
    """Mag-insert og row sa audit_log table - ang Activity Log tab dinhi
    mismo mubasa, so kada importante nga aksyon (add/edit/delete/checkout/
    login) angay mutawag ani. Mapakyas man mag-log ra, dili ni ipahunong
    ang tinuod nga aksyon.

    target_name kay snapshot sa label SA PANAHON sa aksyon (dili re-joined
    later - mahimong blangko na kung na-delete/na-rename na ang row).

    conn (optional): pasa-i ug already-open connection aron dili na mag-
    open og bag-o (skip ang connect() + full PRAGMA setup - real cost na
    sa Pi's SD card kung gina-usab-usab sa matag mutating request). Kung
    gi-supply, ang caller ang responsible mo-close niini human - dinhi ra
    mag-commit sa iyang kaugalingong INSERT, dili ni mo-close. Blangko
    (None) gihapon ang default - mag-open/close ug bag-ong connection sa
    kaugalingon, parehas sa daan nga behavior, para sa call sites nga wala
    nay open connection sa panahon nga gitawag ni (e.g. human na-close ang
    route's conn).

    Returns: None - fire-and-forget."""
    owns_conn = conn is None
    try:
        if owns_conn:
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
        if owns_conn and conn is not None:
            conn.close()
