from utils.logger import log_action
from utils.formatters import full_name
from database import get_db_connection
from services.settings_service import get_damage_restriction_threshold


def check_and_lift_restriction_if_resolved(member_id, conn):
    """If this member was auto-restricted and has no more overdue items, lift it.
    (Bisaya: Kung kini nga member na-restrict automatic tungod sa overdue,
    ug wala na siyay overdue nga item karon, tangtangon/liftan ang
    restriction niya.)

    Args:
        member_id: id of the member to re-check.
        (Bisaya: id sa member nga susihon pag-usab.)
        conn: open sqlite3.Connection - caller owns the transaction; this
            function commits its own UPDATE when it lifts a restriction.
        (Bisaya: bukas nga koneksyon sa database - ang nagtawag ang
        responsable sa transaction; kini nga function mag-commit sa iyang
        kaugalingong UPDATE kung matangtang ang restriction.)

    Returns:
        bool - True if a restriction was actually lifted just now, False if
        the member wasn't auto-restricted, or still has an overdue item.
        (Bisaya: True kung natangtang gyud ang restriction karon; False
        kung ang member wala man diay na-auto-restrict, o naay overdue pa
        gihapon.)
    """
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
            # Name spelled out too, so this log entry reads clearly on its own.
            details=f"System auto-lifted restriction on {member_full_name} (member #{member_id}): no more overdue items",
        )
        return True
    return False


def check_and_restrict_overdue_member(member_id, conn):
    """Restricts this member if they currently have any overdue loan.
    Counterpart to check_and_lift_restriction_if_resolved above - called
    both right after a return (in case that return didn't clear the last
    overdue item) and by sweep_overdue_members() below on a schedule.
    (Bisaya: I-restrict kini nga member kung naay overdue nga loan siya
    karon. Kabaligtad ni sa check_and_lift_restriction_if_resolved sa taas -
    gitawag ni human mag-return - kay basin naay laing overdue pa nga wala
    naka-human - ug gitawag pod sa sweep_overdue_members() sa ubos base sa
    iskedyul.)

    Args:
        member_id: id of the member to check.
        (Bisaya: id sa member nga susihon.)
        conn: open sqlite3.Connection - caller owns the transaction.
        (Bisaya: bukas nga koneksyon sa database - ang nagtawag ang
        responsable sa transaction.)

    Returns:
        bool - True if this member was just restricted (had an overdue
        loan), False if they had none.
        (Bisaya: True kung na-restrict ang member karon - kay naay overdue -
        False kung wala man diay siyay overdue.)
    """
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
    """Damage counterpart to check_and_restrict_overdue_member; restricts after enough non-functional returns.
    Not auto-liftable like overdue - stays restricted until a human resolves it.
    (Bisaya: Kabaligtad para sa "damage" - pareho sa
    check_and_restrict_overdue_member pero base sa daghan nga na-damage nga
    pagbalik sa item. Dili ni ma-auto-lift pareho sa overdue - magpabilin
    ni nga restricted hangtod usa ka tawo/staff ang mo-resolve niini
    mano-mano.)

    Args:
        member_id: id of the member to check.
        (Bisaya: id sa member nga susihon.)
        conn: open sqlite3.Connection - caller owns the transaction.
        (Bisaya: bukas nga koneksyon sa database - ang nagtawag ang
        responsable sa transaction.)

    Returns:
        bool - True if this member was just restricted for damage, False if
        they weren't (already restricted, or below the configured
        damage-count threshold).
        (Bisaya: True kung na-restrict karon ang member tungod sa damage,
        False kung wala - naka-restrict na siya kaniadto, o wala pa
        makaabot sa gi-set nga threshold/limit.)
    """
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
    """Proactively restricts every member with an overdue loan, not just at checkout time.
    Runs on a schedule (app.py); opens its own connection like checkout_service.py's cleanup job.
    (Bisaya: Mag-restrict nang daan sa TANANG member nga naay overdue nga
    loan, dili lang mag-hulat nga mag-checkout pa sila para ma-check. Naa
    ni sa iskedyul (app.py); naay kaugalingong database connection pareho
    sa checkout_service.py's cleanup job.

    Walay parameter ug walay return value.)"""
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
