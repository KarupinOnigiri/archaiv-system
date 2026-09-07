"""Preventive maintenance scheduling - services/asset_service.py's
get_maintenance_due_assets/resolve_maintenance, services/maintenance_service.py's
sweep_maintenance_due, and their notifications/dashboard/reports wiring.

Two independent triggers, either or both optional per-asset: date-based
(next_maintenance_due) and usage-based (maintenance_interval_checkouts vs
checkouts_since_maintenance, incremented on every commit-checkout - see
services/transaction_service.py's finish_checkout_session).
"""

from datetime import date, timedelta

from services.asset_service import get_maintenance_due_assets, resolve_maintenance, get_asset_stat_cards
from services.maintenance_service import sweep_maintenance_due
from tests.conftest import make_asset
from tests.test_assets import set_maintenance
from tests.test_transactions import start_session, add_item, finish, barcode_of

PAST = (date.today() - timedelta(days=2)).isoformat()
SOON = (date.today() + timedelta(days=3)).isoformat()
FAR = (date.today() + timedelta(days=60)).isoformat()


# # # DUE-DETECTION ---------------------------------------------------------


def test_date_based_due_detection(conn, category_id):
    overdue_id = make_asset(conn, category_id, name="Overdue Drill", next_maintenance_due=PAST)
    soon_id = make_asset(conn, category_id, name="Soon Drill", next_maintenance_due=SOON)
    far_id = make_asset(conn, category_id, name="Far Drill", next_maintenance_due=FAR)
    make_asset(conn, category_id, name="No Schedule")

    due = {a["id"] for a in get_maintenance_due_assets(conn, within_days=7)}
    assert overdue_id in due
    assert soon_id in due
    assert far_id not in due


def test_interval_based_due_detection(conn, category_id):
    due_id = make_asset(conn, category_id, name="Overused Saw", maintenance_interval_checkouts=5)
    conn.execute("UPDATE assets SET checkouts_since_maintenance = 5 WHERE id = ?", (due_id,))
    conn.commit()
    not_due_id = make_asset(conn, category_id, name="Fresh Saw", maintenance_interval_checkouts=5)
    conn.execute("UPDATE assets SET checkouts_since_maintenance = 2 WHERE id = ?", (not_due_id,))
    conn.commit()

    due = {a["id"] for a in get_maintenance_due_assets(conn, within_days=7)}
    assert due_id in due
    assert not_due_id not in due


def test_due_detection_excludes_assets_already_in_maintenance(conn, category_id):
    asset_id = make_asset(conn, category_id, next_maintenance_due=PAST)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset_id,))
    conn.commit()

    due = {a["id"] for a in get_maintenance_due_assets(conn, within_days=7)}
    assert asset_id not in due


def test_due_detection_excludes_soft_deleted(conn, category_id):
    asset_id = make_asset(conn, category_id, next_maintenance_due=PAST)
    conn.execute("UPDATE assets SET deleted_at = datetime('now') WHERE id = ?", (asset_id,))
    conn.commit()

    due = {a["id"] for a in get_maintenance_due_assets(conn, within_days=7)}
    assert asset_id not in due


# # # RESOLVE MAINTENANCE ---------------------------------------------------


def test_resolve_maintenance_resets_checkouts_counter(conn, category_id):
    asset_id = make_asset(conn, category_id, maintenance_interval_checkouts=10)
    conn.execute(
        "UPDATE assets SET status = 'maintenance', checkouts_since_maintenance = 10 WHERE id = ?", (asset_id,)
    )
    conn.commit()

    result, error = resolve_maintenance(conn, asset_id, "functional", None, staff_id=None)
    assert error is None, error

    row = conn.execute("SELECT checkouts_since_maintenance FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["checkouts_since_maintenance"] == 0


def test_resolve_maintenance_updates_next_due_date(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional", "next_maintenance_due": FAR},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["next_maintenance_due"] == FAR


# # # STAT CARD --------------------------------------------------------------


def test_maintenance_due_soon_stat_card(conn, category_id):
    make_asset(conn, category_id, next_maintenance_due=PAST)
    make_asset(conn, category_id, next_maintenance_due=FAR)
    make_asset(conn, category_id)

    cards = get_asset_stat_cards(conn)
    assert cards["stat_maintenance_due_soon"] == 1


# # # CHECKOUT INCREMENTS COUNTER -------------------------------------------


def test_commit_checkout_increments_checkouts_since_maintenance(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    a_id = make_asset(conn, category_id, maintenance_interval_checkouts=5)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", a_id)
    add_item(client, csrf, session_id, asset_code)
    resp = finish(client, csrf, session_id)
    assert resp.status_code == 201, resp.get_json()

    row = conn.execute("SELECT checkouts_since_maintenance FROM assets WHERE id = ?", (a_id,)).fetchone()
    assert row["checkouts_since_maintenance"] == 1


# # # SWEEP / NOTIFICATIONS --------------------------------------------------


def test_sweep_creates_maintenance_due_notification(conn, category_id):
    make_asset(conn, category_id, name="Overdue Sander", next_maintenance_due=PAST)
    sweep_maintenance_due()

    notif = conn.execute("SELECT * FROM notifications WHERE type = 'maintenance_due'").fetchone()
    assert notif is not None
    assert "Overdue Sander" in notif["message"]


def test_sweep_is_idempotent(conn, category_id):
    make_asset(conn, category_id, next_maintenance_due=PAST)
    sweep_maintenance_due()
    sweep_maintenance_due()

    count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM notifications WHERE type = 'maintenance_due'"
    ).fetchone()["cnt"]
    assert count == 1


def test_sweep_ignores_assets_not_due(conn, category_id):
    make_asset(conn, category_id, next_maintenance_due=FAR)
    sweep_maintenance_due()

    count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM notifications WHERE type = 'maintenance_due'"
    ).fetchone()["cnt"]
    assert count == 0


# # # REPORTS ROUTE -----------------------------------------------------------


def test_maintenance_schedule_route(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, name="Due Item", next_maintenance_due=PAST)
    # Also gets a schedule now - every new serialized asset auto-fills
    # next_maintenance_due from the global default interval (migration
    # 0059) when no date is set by hand, so "no schedule" no longer means
    # "excluded from this report", just "sorted after ones that are due".
    make_asset(conn, category_id, name="Auto-Scheduled Item")

    resp = client.get("/api/v1/stats/assets/maintenance-schedule")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 2
    assert data["results"][0]["name"] == "Due Item"


def test_maintenance_schedule_export_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/stats/export?sections=maintenance_schedule&format=csv")
    assert resp.status_code == 400
    assert "No exportable sections" in resp.get_json()["error"]


# # # SETTINGS (admin/in_charge-editable defaults) ----------------------------


def update_maintenance_settings(client, csrf, interval_days=180, lead_days=7):
    return client.post(
        "/api/v1/maintenance-settings/update",
        json={"default_maintenance_interval_days": interval_days, "maintenance_reminder_lead_days": lead_days},
        headers={"X-CSRF-Token": csrf},
    )


def test_maintenance_settings_manage_form_shows_current_values(as_admin, conn):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/maintenance-settings/manage-form")
    assert resp.status_code == 200
    assert b"180" in resp.data
    assert b"7" in resp.data


def test_maintenance_settings_update_success(as_admin, conn):
    from services.settings_service import get_default_maintenance_interval_days, get_maintenance_reminder_lead_days

    client, csrf, _ = as_admin
    resp = update_maintenance_settings(client, csrf, interval_days=90, lead_days=14)
    assert resp.status_code == 200
    assert get_default_maintenance_interval_days(conn) == 90
    assert get_maintenance_reminder_lead_days(conn) == 14


def test_maintenance_settings_update_allowed_for_in_charge(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = update_maintenance_settings(client, csrf, interval_days=200, lead_days=10)
    assert resp.status_code == 200


def test_maintenance_settings_update_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = update_maintenance_settings(client, csrf)
    assert resp.status_code == 403


def test_maintenance_settings_rejects_interval_out_of_range(as_admin):
    client, csrf, _ = as_admin
    resp = update_maintenance_settings(client, csrf, interval_days=0, lead_days=7)
    assert resp.status_code == 400
    resp = update_maintenance_settings(client, csrf, interval_days=99999, lead_days=7)
    assert resp.status_code == 400


def test_maintenance_settings_rejects_lead_days_out_of_range(as_admin):
    client, csrf, _ = as_admin
    resp = update_maintenance_settings(client, csrf, interval_days=180, lead_days=-1)
    assert resp.status_code == 400
    resp = update_maintenance_settings(client, csrf, interval_days=180, lead_days=999)
    assert resp.status_code == 400


def test_maintenance_settings_rejects_non_integer(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/maintenance-settings/update",
        json={"default_maintenance_interval_days": "soon", "maintenance_reminder_lead_days": 7},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_maintenance_settings_update_logs_action(as_admin, conn):
    client, csrf, staff_id = as_admin
    update_maintenance_settings(client, csrf, interval_days=120, lead_days=5)
    log = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'updated_maintenance_settings'"
    ).fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id


def test_maintenance_settings_changes_new_asset_default(as_admin, conn, category_id):
    """End-to-end: changing the interval actually changes what a
    newly-created asset with no explicit due date gets auto-scheduled to."""
    from services.asset_service import _default_next_maintenance_due

    client, csrf, _ = as_admin
    update_maintenance_settings(client, csrf, interval_days=30, lead_days=7)

    computed = _default_next_maintenance_due(conn, None, None)
    expected = (date.today() + timedelta(days=30)).isoformat()
    assert computed == expected
