from utils.logger import log_action


def check_and_restrict_overdue_member(member_id, conn):
    overdue = conn.execute(
        """
        SELECT * FROM transactions
        WHERE member_id = ? AND status = 'borrowed'
        AND due_date IS NOT NULL AND due_date < CURRENT_TIMESTAMP
        """,
        (member_id,),
    ).fetchone()

    if overdue:
        conn.execute(
            "UPDATE members SET is_restricted = 1, restriction_reason = ? WHERE id = ?",
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
