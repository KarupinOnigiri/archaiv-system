def get_pending_approvals_count(conn, staff_id, role):
    """Pending unrestriction + deactivation count for the Approvals badge.
    Admin/in_charge see every unrestriction request; tool_keeper sees only their own.
    (Bisaya: Ihap sa pending nga unrestriction requests + deactivation
    requests, gipakita sa Approvals badge sa sidebar. Ang admin/in_charge
    makakita sa TANANG unrestriction request; ang tool_keeper makakita ra sa
    iyang kaugalingong gi-file.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        staff_id: id of the staff member viewing the badge - only used to
            scope the count down for a tool_keeper.
        (Bisaya: id sa staff nga nagtan-aw sa badge - gigamit ra kung
        tool_keeper, para isip lang ang iyang kaugalingong requests.)
        role: that staff member's role string ("admin", "in_charge",
            "tool_keeper").
        (Bisaya: role sa maong staff - "admin", "in_charge", o
        "tool_keeper".)

    Returns:
        int - total pending count (unrestriction + deactivation combined)
        to show on the badge.
        (Bisaya: numero - total nga pending count (unrestriction ug
        deactivation gitapok) nga ipakita sa badge.)
    """
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
