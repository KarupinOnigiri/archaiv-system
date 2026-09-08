from database import get_db_connection
import logging

logger = logging.getLogger(__name__)


def log_action(
    staff_id, action, target_table=None, target_id=None, details=None, target_name=None
):
    """Writes one row to the audit_log table - the Activity Log tab reads
    straight from this table, so every action a staff member takes that's
    worth remembering (add/edit/delete, checkout, login, etc.) should call
    this. Failures here are swallowed (logged, not raised) so a broken audit
    write never blocks the real action it's describing.
    (Bisaya: Mo-butang ug usa ka row sa audit_log table - ang Activity Log
    tab dinhi mismo mubasa. Kada importante nga aksyon sa staff - pananglitan
    pagdugang, pag-usab, pagtangtang, pag-checkout, pag-login - kinahanglan
    mutawag ani. Kung mapakyas ang pagsulat sa audit log, i-log ra kini,
    dili ni ipahunong ang tinuod nga aksyon nga gina-report niini.)

    Args:
        staff_id: id of the staff member who performed the action (or None
            for a system/automated action, e.g. the overdue sweep).
        (Bisaya: id sa staff nga naghimo sa aksyon, o None kung ang system
        mismo/automatic ang naghimo, e.g. ang automatic overdue check.)
        action: short machine-readable label, e.g. "created_asset",
            "marked_for_maintenance" - matches what the Activity Log's
            filter/display expects.
        (Bisaya: mubo nga label para sa aksyon, e.g. "created_asset" -
        kini ang gina-basa sa Activity Log para sa filter ug pagpakita.)
        target_table: which table the action happened on (e.g. "assets"),
            optional.
        (Bisaya: unsa nga table ang naapektuhan, e.g. "assets" - pwede walay
        laman.)
        target_id: the row id in target_table, optional.
        (Bisaya: ang id sa row nga naapektuhan, pwede walay laman.)
        details: free-text extra context for this one entry, optional.
        (Bisaya: dugang nga detalye/paghisgot bahin ani nga entry, pwede
        walay laman.)
        target_name: a snapshot of the target's label AT THE TIME of the
            action, not something re-joined later (which would go blank for
            deleted/renamed/cleaned-up rows). Pass it whenever you have it
            in scope; the Activity Log falls back to "table #id" when it's
            None.
        (Bisaya: usa ka "snapshot" sa ngalan/label sa target SA PANAHON nga
        nahitabo ang aksyon - dili kini gikuha pag-usab human niana, kay
        mahimong wala nay laman kung na-delete/na-rename/na-cleanup na ang
        row. I-pasa kini kung naa ra kanimo; kung wala, ang Activity Log
        mo-gamit na lang sa "table #id".)

    Returns:
        None - this is fire-and-forget; check the app log if you need to
        confirm a write actually succeeded.
        (Bisaya: Walay gibalik - "fire-and-forget" ra ni; tan-awa ang app
        log kung kinahanglan nimo kompirmahon nga na-sulat gyud.)
    """
    conn = None
    try:
        conn = get_db_connection()
        conn.execute(
            """
            INSERT INTO audit_log (staff_id, action, target_table, target_id, details, target_name)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (staff_id, action, target_table, target_id, details, target_name),
        )
        conn.commit()
    except Exception as e:
        logger.warning(
            f"Audit log write failed (action='{action}', "
            f"target_table='{target_table}', target_id={target_id}): {e}"
        )
    finally:
        if conn is not None:
            conn.close()
