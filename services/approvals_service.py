def get_pending_approvals_count(conn, staff_id, role):
    """Combined count of pending unrestriction + admin-deactivation requests
    this staff member can act on, for the "Approvals" sidebar badge.
    admin/in_charge see every pending unrestriction request; tool_keeper
    only sees the ones they filed. Deactivation requests are always counted
    in full for everyone -- any in_charge/tool_keeper can review any of
    them now, and admin/in_charge get the same supervisory view either
    way (see is_eligible_reviewer)."""
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
