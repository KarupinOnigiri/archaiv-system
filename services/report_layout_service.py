import json
from database import get_db_connection
from services.report_widgets import widget_ids_for_role, is_widget_allowed


def get_report_layout(staff_id, role):
    """I-sagol ang na-save nga layout sa mga widget nga allowed para sa
    role - tangtangon ang stale nga id, idugang ang bag-o. Auto-sync ang
    layout sa staff bisan wala pa niya gi-usab manually.

    Returns: list[dict] {"widget_id", "visible", "position"} matag
    entry, in display order."""
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
    """I-save ang han-ay/visibility sa mga widget; tangtangon ang widget
    id nga dili tinuod nga tugotan sa maong role - proteksyon batok sa
    pag-pilit ug widget nga dili unta makita sa staff.

    Returns: list[dict] - ang na-clean nga layout nga na-save."""
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
    """Tangtangon ang na-save nga layout ani nga staff, para mobalik sa
    default nga widgets/order sa sunod nga get_report_layout()."""
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM report_layouts WHERE staff_id = ?", (staff_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
