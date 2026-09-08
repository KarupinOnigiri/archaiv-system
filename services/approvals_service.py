def get_pending_approvals_count(conn, staff_id, role):
    """Ihap sa pending unrestriction + deactivation requests, para sa
    Approvals badge sa sidebar. Ang admin/in_charge makakita sa TANANG
    request; ang tool_keeper makakita ra sa iyang kaugalingon.

    Returns: int - total pending count."""
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
