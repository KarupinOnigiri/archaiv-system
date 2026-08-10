from utils.logger import log_action
from services.seed_service import seed_initial_admin
from utils.formatters import full_name

# Any active in_charge/tool_keeper account can vote on any pending request now --
# no more role == required_tier gating. Only these two roles vote; other admins don't.
ELIGIBLE_REVIEWER_ROLES = ("in_charge", "tool_keeper")


def is_eligible_reviewer(role):
    """Whether this role can cast an approve/deny vote on an admin-deactivation request."""
    return role in ELIGIBLE_REVIEWER_ROLES


def determine_approval_tier(conn, requester_id, target_id):
    """Decides how big a consensus a new admin-deactivation request needs,
    based on how many active in_charge/tool_keeper accounts actually exist
    right now. Snapshotted once at request creation, never recalculated.

    Tiers (first one actually reachable wins):
      full:    >=2 active in_charge and >=4 in_charge+tool_keeper combined -> 4 approvals, 2 of them in_charge
      reduced: >=1 active in_charge and >=3 combined -> 3 approvals, 1 in_charge
      minimal: >=1 active in_charge and >=2 combined -> 2 approvals, 1 in_charge

    requester_id/target_id are kept for call-site compatibility -- both are
    always admins, so they'd never show up in the in_charge/tool_keeper
    counts anyway, unlike the old admin-tier check this replaces.

    Returns (tier, required_approvals, required_in_charge), or (None, None, None)
    if there simply aren't enough in_charge/tool_keeper accounts to ever satisfy it."""
    active_in_charge = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'in_charge' AND is_active = 1"
    ).fetchone()["cnt"]
    active_tool_keepers = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'tool_keeper' AND is_active = 1"
    ).fetchone()["cnt"]
    combined = active_in_charge + active_tool_keepers

    if active_in_charge >= 2 and combined >= 4:
        return "full", 4, 2
    if active_in_charge >= 1 and combined >= 3:
        return "reduced", 3, 1
    if active_in_charge >= 1 and combined >= 2:
        return "minimal", 2, 1
    return None, None, None


def get_review_counts(conn, request_id):
    """(approve_count, in_charge_approve_count, deny_count) for a request --
    the numbers finalize eligibility is checked against."""
    row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN a.decision = 'approve' THEN 1 ELSE 0 END) AS approve_count,
            SUM(CASE WHEN a.decision = 'approve' AND s.role = 'in_charge' THEN 1 ELSE 0 END) AS in_charge_approve_count,
            SUM(CASE WHEN a.decision = 'deny' THEN 1 ELSE 0 END) AS deny_count
        FROM admin_deactivation_approvals a
        JOIN staff s ON s.id = a.approver_staff_id
        WHERE a.request_id = ?
        """,
        (request_id,),
    ).fetchone()
    return (row["approve_count"] or 0, row["in_charge_approve_count"] or 0, row["deny_count"] or 0)


def requirements_met(request_row, conn):
    """Whether a request has enough approve votes (and enough of them from
    in_charge) to be eligible for the requester to finalize -- doesn't mean
    it WILL be finalized, that's still the requester's own call."""
    approve_count, in_charge_count, _ = get_review_counts(conn, request_row["id"])
    return (
        approve_count >= request_row["required_approvals"]
        and in_charge_count >= request_row["required_in_charge"]
    )


def finalize_admin_deactivation(request_row, actor_staff_id, conn):
    """Actually deactivates the target admin account. Only ever called from
    the requester-triggered /finalize route -- reaching the required
    approval count no longer deactivates anything by itself; the requester
    reviews every vote and its reason first, then pulls the trigger
    themselves (or not). Returns (ok, error_message).

    Still refuses if the target is the last active admin -- that floor is
    a hard system invariant, not something peer approval or the requester's
    own judgment gets to override."""
    if not requirements_met(request_row, conn):
        return False, "The required approvals haven't been reached yet"

    target_staff_row = conn.execute(
        """
        SELECT staff.username, members.first_name, members.last_name, members.middle_name, members.suffix
        FROM staff JOIN members ON members.id = staff.member_id
        WHERE staff.id = ?
        """,
        (request_row["target_staff_id"],),
    ).fetchone()
    target_username = target_staff_row["username"] if target_staff_row else None
    target_label = (
        full_name(
            target_staff_row["first_name"],
            target_staff_row["last_name"],
            target_staff_row["middle_name"],
            target_staff_row["suffix"],
        )
        if target_staff_row
        else f"staff #{request_row['target_staff_id']}"
    )

    remaining_admins = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 AND id != ?",
        (request_row["target_staff_id"],),
    ).fetchone()["cnt"]

    if remaining_admins == 0:
        return False, f"Can't deactivate {target_label} -- they're the last active admin account"

    conn.execute(
        "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL, deactivated_at = datetime('now', 'localtime') WHERE id = ?",
        (request_row["target_staff_id"],),
    )
    conn.execute(
        """
        UPDATE admin_deactivation_requests
        SET status = 'approved', resolved_at = datetime('now', 'localtime')
        WHERE id = ?
        """,
        (request_row["id"],),
    )
    # inline import to dodge a circular import with notification_service
    from services.notification_service import create_admin_deactivation_resolved_notification

    create_admin_deactivation_resolved_notification(
        conn, request_row["id"], request_row["requested_by_staff_id"], target_label, approved=True
    )
    conn.commit()

    log_action(
        staff_id=actor_staff_id,
        action="deactivated_admin_via_approval",
        target_table="staff",
        target_id=request_row["target_staff_id"],
        target_name=target_username,
        details=(
            f"Admin account {target_label} deactivated by the requester after reaching "
            f"{request_row['required_approvals']} approval(s), {request_row['required_in_charge']}+ "
            f"in_charge (request #{request_row['id']})"
        ),
    )

    # re-seed the temporary setup admin now if that just left zero active admins
    seed_initial_admin()

    return True, None
