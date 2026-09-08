from utils.logger import log_action
from services.seed_service import seed_initial_admin
from utils.formatters import full_name

# Any active in_charge/tool_keeper can vote now - other admins can't.
ELIGIBLE_REVIEWER_ROLES = ("in_charge", "tool_keeper")


def is_eligible_reviewer(role):
    """Whether this role can cast an approve/deny vote on an admin-deactivation request.
    (Bisaya: Kung pwede ba ni nga role mo-vote - approve o deny - sa usa ka
    admin-deactivation request.)

    Args:
        role: a staff role string, e.g. "admin", "in_charge", "tool_keeper".
        (Bisaya: usa ka role sa staff, e.g. "admin", "in_charge",
        "tool_keeper".)

    Returns:
        bool - True only for "in_charge" and "tool_keeper"; other admins
        can't vote on this (an admin can't vote another admin's
        deactivation).
        (Bisaya: True para ra sa "in_charge" ug "tool_keeper"; ang laing
        admin dili pwede mo-vote - dili pwede ang admin mo-vote sa
        pag-deactivate sa laing admin.)
    """
    return role in ELIGIBLE_REVIEWER_ROLES


def determine_approval_tier(conn, requester_id, target_id):
    """Picks an approval tier (full/reduced/minimal) from how many in_charge/tool_keeper accounts are active.
    Snapshotted once at request creation, never recalculated.
    requester_id/target_id are unused here, kept only for call-site compatibility.
    (Bisaya: Mopili ug "approval tier" - full/reduced/minimal - base sa
    kadaghan sa active nga in_charge/tool_keeper accounts karon. Kini
    gikuha usa ra ka beses - "snapshot" - sa dihang gihimo ang request,
    dili na ni ma-recalculate/usbon pag-usab bisan mausab pa ang
    kadaghanon sa staff human niana. Ang requester_id/target_id wala gigamit
    dinhi, gibutang ra para pareho ang porma sa pagtawag/call-site.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        requester_id: unused (kept for call-site compatibility).
        (Bisaya: wala gigamit - gibutang ra para pareho ang porma sa
        pagtawag.)
        target_id: unused (kept for call-site compatibility).
        (Bisaya: wala gigamit - gibutang ra para pareho ang porma sa
        pagtawag.)

    Returns:
        tuple (tier, required_approvals, required_in_charge) - `tier` is
        one of "full"/"reduced"/"minimal", or (None, None, None) when
        there aren't even enough active reviewers to allow the request at
        all.
        (Bisaya: usa ka tuple (tier, required_approvals,
        required_in_charge) - ang `tier` usa sa "full"/"reduced"/
        "minimal", o (None, None, None) kung wala pay igo nga active nga
        reviewer para tugotan gani ang request.)
    """
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
    """(approve, in_charge_approve, deny) counts for a request's finalize-eligibility check.
    (Bisaya: Ihap sa (approve, in_charge_approve, deny) para sa
    finalize-eligibility check sa usa ka request - kung igo na ba ang boto
    para ma-finalize.)

    Args:
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)
        request_id: id of the admin_deactivation_requests row to count
            votes for.
        (Bisaya: id sa request nga ihapon ang mga boto.)

    Returns:
        tuple (approve_count, in_charge_approve_count, deny_count) - all
        ints, 0 if nobody's voted yet.
        (Bisaya: usa ka tuple (approve_count, in_charge_approve_count,
        deny_count) - tanan numero, 0 kung wala pay nivoto.)
    """
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
    """Whether a request has enough votes to be finalize-eligible; finalizing itself is still manual.
    (Bisaya: Kung igo na ba ang boto sa usa ka request para mahimo na
    "finalize-eligible" - apan ang tinuod nga finalize kinahanglan gihapon
    buhaton mano-mano/manual sa requester.)

    Args:
        request_row: the admin_deactivation_requests row (needs
            required_approvals and required_in_charge columns).
        (Bisaya: ang row sa admin_deactivation_requests - kinahanglan naa
        ni'y required_approvals ug required_in_charge nga column.)
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        bool - True when both the total-approval and in_charge-approval
        thresholds are met.
        (Bisaya: True kung na-abot na ang parehong threshold - total nga
        approval ug in_charge nga approval.)
    """
    approve_count, in_charge_count, _ = get_review_counts(conn, request_row["id"])
    return (
        approve_count >= request_row["required_approvals"]
        and in_charge_count >= request_row["required_in_charge"]
    )


def finalize_admin_deactivation(request_row, actor_staff_id, conn):
    """Deactivates the target admin. Only called from the requester-triggered /finalize route.
    Refuses if the target is the last active admin - a hard system invariant.
    (Bisaya: I-deactivate ang gina-target nga admin. Gitawag ra ni gikan sa
    /finalize route nga gi-trigger sa requester mismo - dili sa mga
    reviewer nga ni-vote. Mo-refuse kung ang target mao ang KATAPUSANG
    active nga admin - kini usa ka hugot/hard nga patakaran sa system, dili
    gyud mahitabo nga mawad-an og admin ang tibuok sistema.)

    Args:
        request_row: the admin_deactivation_requests row to finalize -
            needs id, target_staff_id, requested_by_staff_id,
            required_approvals, required_in_charge.
        (Bisaya: ang row sa request nga i-finalize - kinahanglan naa ni'y
        id, target_staff_id, requested_by_staff_id, required_approvals,
        required_in_charge.)
        actor_staff_id: id of the staff member clicking Finalize (the
            original requester), used for the audit log entry.
        (Bisaya: id sa staff nga nag-click sa Finalize - ang orihinal nga
        requester - gigamit ra para sa audit log.)
        conn: open sqlite3.Connection.
        (Bisaya: bukas nga koneksyon sa database.)

    Returns:
        tuple (success, error) - (True, None) when the target admin was
        deactivated; (False, "...") with a human-readable reason when
        requirements_met() fails or the target is the last active admin.
        (Bisaya: usa ka tuple (success, error) - (True, None) kung
        malampuson nga na-deactivate ang target admin; (False, "...") naay
        klaro nga rason kung wala pa igo ang requirements, o kung target
        mao ang katapusang active nga admin.)
    """
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
