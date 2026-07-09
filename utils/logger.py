from database import get_db_connection


def log_action(staff_id, action, target_table=None, target_id=None, details=None):
    conn = get_db_connection()
    conn.execute(
        """
        INSERT INTO audit_log (staff_id, action, target_table, target_id, details)
        VALUES (?, ?, ?, ?, ?)
        """,
        (staff_id, action, target_table, target_id, details),
    )
    conn.commit()
    conn.close()
