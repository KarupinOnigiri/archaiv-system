from utils.logger import log_action
from services.seed_service import seed_initial_admin


def determine_approval_tier(conn, requester_id, target_id):
    """Decides which staff tier must approve a pending admin-deactivation
    request, based on staff counts at request-creation time. Snapshotted
    once and stored on the request row — never recalculated later.

    Tiers, in order (first available wins):
      - admin:       a third active admin exists (not requester, not
                      target) -> 1 approval needed
      - in_charge:   fewer than 3 admins total, but >= 2 active
                      in_charge accounts exist -> 2 approvals needed
      - tool_keeper: fewer than 2 active in_charge accounts exist, but
                      >= 1 active tool_keeper exists -> 1 approval needed

    Returns (tier, required_approvals), or (None, None) if no tier
    currently has enough eligible people to ever satisfy it.
    """
    other_active_admins = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM staff
        WHERE role = 'admin' AND is_active = 1 AND id NOT IN (?, ?)
        """,
        (requester_id, target_id),
    ).fetchone()["cnt"]

    if other_active_admins >= 1:
        return "admin", 1

    active_in_charge = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'in_charge' AND is_active = 1"
    ).fetchone()["cnt"]

    if active_in_charge >= 2:
        return "in_charge", 2

    active_tool_keepers = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'tool_keeper' AND is_active = 1"
    ).fetchone()["cnt"]

    if active_tool_keepers >= 1:
        return "tool_keeper", 1

    return None, None


def finalize_if_ready(request_row, conn):
    """Checks whether a pending admin-deactivation request has reached its
    required approval count, and if so, deactivates the target admin
    account and marks the request resolved. Returns True if finalized,
    False if still waiting on more approvals.

    Refuses to finalize if the target is the last active admin — approval
    consensus governs *who* can deactivate an admin, not *whether the
    system can be left with zero admins*, so that floor is enforced here
    regardless of how many approvals came in."""
    approval_count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM admin_deactivation_approvals WHERE request_id = ?",
        (request_row["id"],),
    ).fetchone()["cnt"]

    if approval_count < request_row["required_approvals"]:
        return False

    remaining_admins = conn.execute(
        "SELECT COUNT(*) AS cnt FROM staff WHERE role = 'admin' AND is_active = 1 AND is_system_seeded = 0 AND id != ?",
        (request_row["target_staff_id"],),
    ).fetchone()["cnt"]

    if remaining_admins == 0:
        conn.execute(
            """
            UPDATE admin_deactivation_requests
            SET status = 'denied', denial_reason = ?, resolved_at = datetime('now', 'localtime')
            WHERE id = ?
            """,
            (
                "Automatically denied: target is the last active admin account",
                request_row["id"],
            ),
        )
        conn.commit()

        log_action(
            staff_id=None,
            action="admin_deactivation_auto_denied",
            target_table="admin_deactivation_requests",
            target_id=request_row["id"],
            details=(
                f"Request #{request_row['id']} auto-denied on finalization: "
                f"target {request_row['target_staff_id']} is the last active admin"
            ),
        )
        return False

    conn.execute(
        "UPDATE staff SET is_active = 0, assigned_start = NULL, assigned_end = NULL WHERE id = ?",
        (request_row["target_staff_id"],),
    )
    conn.commit()

    log_action(
        staff_id=None,
        action="deactivated_admin_via_approval",
        target_table="staff",
        target_id=request_row["target_staff_id"],
        details=(
            f"Admin account deactivated after reaching {request_row['required_approvals']} "
            f"{request_row['required_tier']} approval(s) (request #{request_row['id']})"
        ),
    )

    # If that deactivation just left the system with zero active real
    # admins, seed the temporary setup admin immediately rather than
    # waiting for the next app restart.
    seed_initial_admin()

    return True
