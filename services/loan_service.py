from utils.logger import log_action


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
            details=f"Auto-lifted restriction on member {member_id}: no more overdue items",
        )
        return True
    return False


def check_and_restrict_overdue_member(member_id, conn):
    overdue = conn.execute(
        """
        SELECT * FROM transactions
        WHERE member_id = ? AND status = 'borrowed'
        AND due_date IS NOT NULL AND due_date < datetime('now', 'localtime')
        """,
        (member_id,),
    ).fetchone()

    if overdue:
        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'auto' WHERE id = ?",
            ("Has an overdue tool that has not been returned", member_id),
        )
        conn.commit()

        # auto-restriction, no staff_id since no human triggered this
        log_action(
            staff_id=None,
            action="auto_restricted_member",
            target_table="members",
            target_id=member_id,
            details=f"Auto-restricted member {member_id}: overdue transaction {overdue['id']}",
        )
        return True
    return False
