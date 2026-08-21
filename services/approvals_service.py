def get_pending_approvals_count(conn, staff_id, role):
    """Pending unrestriction + deactivation count for the Approvals badge.
    Admin/in_charge see every unrestriction request; tool_keeper sees only their own."""
    if role in ("admin", "in_charge"):
        unrestriction_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM unrestriction_requests WHERE status = 'pending'"
        ).fetchone()["cnt"]
    else:
        unrestriction_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM unrestriction_requests WHERE status = 'pending' AND requested_by_staff_id = ?",
            (staff_id,),
        ).fetchone()["cnt"]

    deactivation_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_deactivation_requests WHERE status = 'pending'"
    ).fetchone()["cnt"]

    return unrestriction_count + deactivation_count
