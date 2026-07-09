from database import get_db_connection


def log_action(staff_id, action, target_table=None, target_id=None, details=None):
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO audit_log (staff_id, action, target_table, target_id, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (staff_id, action, target_table, target_id, details),
        )
        conn.commit()
    except Exception as e:
        print(
            f"WARNING: audit log write failed (action='{action}', "
            f"target_table='{target_table}', target_id={target_id}): {e}"
        )
    finally:
        if conn is not None:
            conn.close()
