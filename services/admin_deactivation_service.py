from utils.logger import log_action
from services.seed_service import seed_initial_admin
from utils.formatters import full_name

# Any active in_charge/tool_keeper can vote now - other admins can't.
ELIGIBLE_REVIEWER_ROLES = ("in_charge", "tool_keeper")


def is_eligible_reviewer(role):
    """Kung pwede ba ni nga role mo-vote (approve/deny) sa usa ka
    admin-deactivation request. Admin dili pwede mo-vote sa laing admin.

    Returns: bool - True para ra sa "in_charge" ug "tool_keeper"."""
    return role in ELIGIBLE_REVIEWER_ROLES


def determine_approval_tier(conn, requester_id, target_id):
    """Mopili og approval tier (full/reduced/minimal) base sa kadaghan sa
    active in_charge/tool_keeper accounts karon. Snapshot ra ni sa request
    creation, dili na ma-recalculate. requester_id/target_id unused, para
    ra sa call-site compatibility.

    Returns: tuple (tier, required_approvals, required_in_charge) - or
    (None, None, None) kung wala pay igo nga reviewer."""
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
    """Ihap sa (approve, in_charge_approve, deny) para sa
    finalize-eligibility check.

    Returns: tuple (approve_count, in_charge_approve_count, deny_count)."""
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
    """Kung igo na ba ang boto para mahimo "finalize-eligible" - ang
    tinuod nga finalize kinahanglan gihapon manual sa requester.

    Returns: bool."""
    approve_count, in_charge_count, _ = get_review_counts(conn, request_row["id"])
    return (
        approve_count >= request_row["required_approvals"]
        and in_charge_count >= request_row["required_in_charge"]
    )


def finalize_admin_deactivation(request_row, actor_staff_id, conn):
    """I-deactivate ang target admin. Gitawag ra gikan sa /finalize route
    nga gi-trigger sa requester mismo. Mo-refuse kung target mao ang
    KATAPUSANG active admin - hard nga patakaran sa system.

    Returns: tuple (success, error)."""
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
        return False, f"Can't deactivate {target_label} - they're the last active admin account"

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
        conn=conn,
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

    # Re-seeds the temp setup admin if that just left zero active admins.
    seed_initial_admin()

    return True, None
