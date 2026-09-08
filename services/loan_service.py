from utils.logger import log_action
from utils.formatters import full_name
from database import get_db_connection
from services.settings_service import get_damage_restriction_threshold


def check_and_lift_restriction_if_resolved(member_id, conn):
    """Kung na-auto-restrict ni nga member ug wala nay overdue item karon,
    tangtangon ang restriction niya.

    Returns: bool - True kung na-lift gyud ang restriction karon."""
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
            conn=conn,
            staff_id=None,
            action="auto_unrestricted_member",
            target_table="members",
            target_id=member_id,
            target_name=member_full_name,
            # Name spelled out too, so this log entry reads clearly on its own.
            details=f"System auto-lifted restriction on {member_full_name} (member #{member_id}): no more overdue items",
        )
        return True
    return False


def check_and_restrict_overdue_member(member_id, conn):
    """I-restrict kini nga member kung naay overdue nga loan siya karon -
    kabaligtad sa check_and_lift_restriction_if_resolved. Gitawag human
    mag-return ug sa sweep_overdue_members() sa ubos.

    Returns: bool - True kung na-restrict karon (naay overdue)."""
    # Fetched only for a readable Activity Log name, not needed for the check.
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

        # staff_id=None since no human triggered this; name spelled out in the message too
        log_action(
            conn=conn,
            staff_id=None,
            action="auto_restricted_member",
            target_table="members",
            target_id=member_id,
            target_name=member_full_name,
            details=f"System auto-restricted {member_full_name or f'member #{member_id}'} (member #{member_id}): overdue transaction {overdue['id']}",
        )
        return True
    return False


def check_and_restrict_damaged_returns(member_id, conn):
    """Damage version ni sa check_and_restrict_overdue_member - i-restrict
    human sa igo nga daghan nga na-damage nga pagbalik. Dili ma-auto-lift
    pareho sa overdue - kinahanglan tawo/staff ang mo-resolve mano-mano.

    Returns: bool - True kung na-restrict karon tungod sa damage."""
    member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
    if member is None or member["is_restricted"]:
        return False

    threshold = get_damage_restriction_threshold(conn)
    damaged_count = conn.execute(
        """
        SELECT COUNT(*) AS cnt FROM transactions
        WHERE member_id = ? AND condition_in IS NOT NULL AND condition_in != 'functional'
        """,
        (member_id,),
    ).fetchone()["cnt"]

    if damaged_count < threshold:
        return False

    member_full_name = full_name(
        member["first_name"], member["last_name"], member["middle_name"], member["suffix"]
    )

    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'auto_damage' WHERE id = ?",
        (f"Returned {damaged_count} damaged/inoperable items", member_id),
    )
    conn.commit()

    log_action(
        conn=conn,
        staff_id=None,
        action="auto_restricted_member_damage",
        target_table="members",
        target_id=member_id,
        target_name=member_full_name,
        details=(
            f"System auto-restricted {member_full_name or f'member #{member_id}'} "
            f"(member #{member_id}): {damaged_count} damaged item returns "
            f"(threshold {threshold})"
        ),
    )
    return True


def sweep_overdue_members():
    """Mag-restrict nang daan sa TANANG member nga naay overdue loan, dili
    lang maghulat sa checkout time. Naa sa iskedyul (app.py), naay
    kaugalingong connection."""
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
