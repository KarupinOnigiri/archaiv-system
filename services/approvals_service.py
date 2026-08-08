def get_pending_approvals_count(conn, staff_id, role):
    """Returns the combined count of pending unrestriction requests,
    pending admin-deactivation requests, and pending admin-promotion
    requests this staff member can currently act on or is tracking --
    feeds the "Approvals" sidebar badge in base.html (via app.py's
    inject_csrf_token context processor), same scoping rules as
    list_unrestriction_requests (routes/requests.py) and
    list_admin_deactivation_requests / list_admin_promotion_requests
    (routes/staff.py):

      - admin/in_charge: every pending request of all three kinds
        (supervisory view -- they can review anyone's).
      - everyone else: only unrestriction requests they filed themselves,
        plus admin-deactivation/promotion requests whose required_tier
        matches their own role (the only ones they're eligible to
        approve/deny)."""
    if role in ("admin", "in_charge"):
        unrestriction_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM unrestriction_requests WHERE status = 'pending'"
        ).fetchone()["cnt"]
        deactivation_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_deactivation_requests WHERE status = 'pending'"
        ).fetchone()["cnt"]
        promotion_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_promotion_requests WHERE status = 'pending'"
        ).fetchone()["cnt"]
    else:
        unrestriction_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM unrestriction_requests WHERE status = 'pending' AND requested_by_staff_id = ?",
            (staff_id,),
        ).fetchone()["cnt"]
        deactivation_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_deactivation_requests WHERE status = 'pending' AND required_tier = ?",
            (role,),
        ).fetchone()["cnt"]
        promotion_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM admin_promotion_requests WHERE status = 'pending' AND required_tier = ?",
            (role,),
        ).fetchone()["cnt"]

    return unrestriction_count + deactivation_count + promotion_count
