from utils.logger import log_action
from utils.formatters import full_name
from database import get_db_connection


def check_and_lift_restriction_if_resolved(member_id, conn):
    """If this member was auto-restricted and has no more overdue items, lift it."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()

    if not member["is_restricted"] or member["restriction_source"] != "auto":
        return False

    still_overdue = conn.execute(
        """
        SELECT * FROM transactions
        WHERE member_id = ? AND status = 'borrowed'
        AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """,
        (member_id,),
    ).fetchone()

    if still_overdue is None:
        member_full_name = full_name(
            member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
        )

        conn.execute(
            "UPDATE members SET is_restricted = 0, restriction_reason = NULL, restriction_source = NULL WHERE id = ?",
            (member_id,),
        )
        conn.commit()

        log_action(
            staff_id=None,
            action="auto_unrestricted_member",
            target_table="members",
            target_id=member_id,
            target_name=member_full_name,
            # Name is spelled out in the message itself, not just the
            # separate TARGET column, so it reads clearly on its own in the
            # Activity Log's details/detail-modal view.
            details=f"System auto-lifted restriction on {member_full_name} (member #{member_id}): no more overdue items",
        )
        return True
    return False


def check_and_restrict_overdue_member(member_id, conn):
    # Fetched up front purely for a readable Activity Log target below --
    # the overdue check itself doesn't need it.
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    overdue = conn.execute(
        """
        SELECT * FROM transactions
        WHERE member_id = ? AND status = 'borrowed'
        AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """,
        (member_id,),
    ).fetchone()

    if overdue:
        member_full_name = (
            full_name(member["first_name"], member["last_name"], member["middle_name"], member["suffix"])
            if member
            else None
        )

        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'auto' WHERE id = ?",
            ("Has an overdue tool that has not been returned", member_id),
        )
        conn.commit()

        # auto-restriction, no staff_id since no human triggered this --
        # name is spelled out in the message itself (not just the TARGET
        # column) so it's unambiguous who was restricted at a glance.
        log_action(
            staff_id=None,
            action="auto_restricted_member",
            target_table="members",
            target_id=member_id,
            target_name=member_full_name,
            details=f"System auto-restricted {member_full_name or f'member #{member_id}'} (member #{member_id}): overdue transaction {overdue['id']}",
        )
        return True
    return False


def sweep_overdue_members():
    """Proactively runs the overdue check across every member who
    currently has an open ('borrowed') loan, instead of relying on
    check_and_restrict_overdue_member only ever being triggered
    opportunistically (at checkout-session start/commit). Without this,
    a member can sit overdue and unrestricted — invisible as "restricted"
    anywhere in the UI — for as long as nobody happens to try checking
    something else out to them.

    Meant to be run on a schedule (see app.py, alongside
    cleanup_stale_checkout_sessions and cleanup_expired_sessions), not
    tied to any particular request. Opens its own connection for that
    reason — same pattern as cleanup_stale_checkout_sessions in
    checkout_service.py — rather than taking one as a parameter like the
    two request-triggered functions above."""
    conn = get_db_connection()
    try:
        overdue_member_ids = conn.execute("""
            SELECT DISTINCT transactions.member_id FROM transactions
            JOIN members ON members.id = transactions.member_id
            WHERE transactions.status = 'borrowed'
            AND transactions.due_date IS NOT NULL
            AND transactions.due_date < datetime('now', 'localtime')
            AND members.is_restricted = 0
            """).fetchall()

        for row in overdue_member_ids:
            check_and_restrict_overdue_member(row["member_id"], conn)
    finally:
        conn.close()
