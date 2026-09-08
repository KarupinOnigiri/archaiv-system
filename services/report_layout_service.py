import json
from database import get_db_connection
from services.report_widgets import widget_ids_for_role, is_widget_allowed


def get_report_layout(staff_id, role):
    """Merges the saved layout with the role's widgets - drops stale ids, appends new ones.
    (Bisaya: I-sagol ang na-save nga layout sa mga widget nga allowed para
    sa role - tangtangon ang mga widget id nga karaan na/wala nay
    kapuslanan, idugang ang mga bag-o. Kung mausab unya ang mga available
    nga widget - e.g. gidugang ang bag-ong feature - kini nga function ang
    naghandle para ma-sync gihapon ang layout sa staff bisan wala pa niya
    gi-usab manually.)

    Args:
        staff_id: id of the staff member whose Reports layout to load.
        (Bisaya: id sa staff kansang Reports layout kuhaon.)
        role: that staff member's role - determines which widgets are
            even allowed to appear (see widget_ids_for_role/is_widget_allowed
            in report_widgets.py).
        (Bisaya: role sa maong staff - mo-determinar kung unsang mga
        widget ang gitugotan gyud makita - tan-awa ang
        widget_ids_for_role/is_widget_allowed sa report_widgets.py.)

    Returns:
        list[dict] - one entry per widget: {"widget_id": str, "visible":
        bool, "position": int}, in display order. Falls back to every
        default widget (visible, in default order) when there's no saved
        layout yet, or the saved one is corrupted.
        (Bisaya: usa ka listahan sa dict - usa ka entry matag widget:
        {"widget_id": str, "visible": bool, "position": int}, sunod sa
        han-ay nga ipakita. Mo-fallback sa default nga tanan-visible nga
        layout kung wala pay na-save, o kung sayop/corrupted ang na-save.)
    """
    default_ids = widget_ids_for_role(role)

    conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT layout_json FROM report_layouts WHERE staff_id = ?", (staff_id,)
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return [
            {"widget_id": wid, "visible": True, "position": i}
            for i, wid in enumerate(default_ids)
        ]

    try:
        saved = json.loads(row["layout_json"])
        if not isinstance(saved, list):
            raise ValueError
    except (ValueError, TypeError):
        saved = []

    saved_by_id = {
        entry.get("widget_id"): entry
        for entry in saved
        if isinstance(entry, dict) and is_widget_allowed(entry.get("widget_id"), role)
    }

    ordered_ids = [wid for wid in saved_by_id if wid in default_ids]
    # Preserve the saved order, then append any new default widget at the end.
    ordered_ids.sort(key=lambda wid: saved_by_id[wid].get("position", 0))
    for wid in default_ids:
        if wid not in saved_by_id:
            ordered_ids.append(wid)

    return [
        {
            "widget_id": wid,
            "visible": bool(saved_by_id[wid].get("visible", True)) if wid in saved_by_id else True,
            "position": i,
        }
        for i, wid in enumerate(ordered_ids)
    ]


def save_report_layout(staff_id, role, layout):
    """Persists widget order/visibility; drops any widget id the role isn't actually allowed to see.
    (Bisaya: I-save ang han-ay/order ug visibility sa mga widget; tangtangon
    ang bisan unsang widget id nga dili tinuod nga tugotan sa maong role -
    proteksyon batok sa usa ka staff nga mo-save/pilit ug widget nga dili
    unta niya makita.)

    Args:
        staff_id: id of the staff member saving this layout.
        (Bisaya: id sa staff nga nag-save ani nga layout.)
        role: that staff member's role - used to filter out disallowed
            widget ids.
        (Bisaya: role sa maong staff - gigamit para sala-on/i-filter ang
        mga widget id nga dili tugotan.)
        layout: list of dicts from the client, e.g.
            [{"widget_id": "...", "visible": True}, ...] - order in the
            list is the display order.
        (Bisaya: listahan sa dict gikan sa browser, e.g. [{"widget_id":
        "...", "visible": True}, ...] - ang han-ay sa listahan mao ang
        han-ay nga ipakita.)

    Returns:
        list[dict] - the cleaned/validated layout that was actually saved
        (widget_id, visible, position for each), same shape as
        get_report_layout()'s return.
        (Bisaya: usa ka listahan sa dict - ang na-limpyohan/na-validate nga
        layout nga tinuod nga na-save, pareho ka porma sa gibalik sa
        get_report_layout().)
    """
    cleaned = [
        {"widget_id": entry["widget_id"], "visible": bool(entry.get("visible", True)), "position": i}
        for i, entry in enumerate(layout)
        if isinstance(entry, dict)
        and isinstance(entry.get("widget_id"), str)
        and is_widget_allowed(entry["widget_id"], role)
    ]

    conn = get_db_connection()
    try:
        conn.execute(
            """
            INSERT INTO report_layouts (staff_id, layout_json, updated_at)
            VALUES (?, ?, datetime('now', 'localtime'))
            ON CONFLICT(staff_id) DO UPDATE SET
                layout_json = excluded.layout_json,
                updated_at = excluded.updated_at
            """,
            (staff_id, json.dumps(cleaned)),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return cleaned


def reset_report_layout(staff_id):
    """Deletes this staff member's saved layout, so Reports falls back to
    the role's default widgets/order next time get_report_layout() runs.
    (Bisaya: Tangtangon ang na-save nga layout ani nga staff, para
    mobalik sa default nga widgets/order sa role sa sunod nga
    get_report_layout() ipadagan.)

    Args:
        staff_id: id of the staff member whose layout to reset.
        (Bisaya: id sa staff kansang layout i-reset.)

    Returns:
        None.
        (Bisaya: walay gibalik.)
    """
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM report_layouts WHERE staff_id = ?", (staff_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
