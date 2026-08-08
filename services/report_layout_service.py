import json
from database import get_db_connection
from services.report_widgets import widget_ids_for_role, is_widget_allowed


def get_report_layout(staff_id, role):
    """Returns this staff member's Reports widget order/visibility as a
    list of {"widget_id", "visible", "position"} dicts, one per widget
    their role is allowed to see -- always the *complete*, role-appropriate
    set, never a partial one.

    Merges whatever was saved (migrations/0032_add_report_layouts.sql)
    with the role's current default order:
      - a saved widget id the role/registry no longer recognizes (an old
        layout from before a widget was renamed/removed, or saved under a
        role the account no longer has) is silently dropped rather than
        surfaced as a broken card.
      - a widget newly added to the registry since this staff member last
        saved a layout appears at the end, visible by default, rather than
        staying invisible forever until they happen to open Customize.
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
    # Preserve the saved relative order, then append any default widget
    # that wasn't in the saved layout at all (newly added since, or never
    # customized) at the end.
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
    """Persists a staff member's widget order/visibility. `layout` is the
    list of {"widget_id", "visible"} dicts the frontend posts after a
    drag/hide change; position is taken from list order rather than a
    caller-supplied index, and any widget id the role isn't allowed to see
    is dropped -- so a tampered request can't smuggle in an admin-only
    widget id for a tool_keeper account just because the client happened
    to send one."""
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
    conn = get_db_connection()
    try:
        conn.execute("DELETE FROM report_layouts WHERE staff_id = ?", (staff_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
