"""Mark for Maintenance (quick action, inverse of Resolve Maintenance) +
the global default-maintenance-interval auto-fill for next_maintenance_due -
services/asset_service.py's mark_for_maintenance / _default_next_maintenance_due.
"""

from datetime import date, timedelta

from services.asset_service import create_asset, mark_for_maintenance, resolve_maintenance
from tests.conftest import make_asset
from tests.test_assets import set_maintenance


def test_mark_for_maintenance_success(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/mark-maintenance",
        json={"notes": "chuck slipping"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    row = conn.execute("SELECT status, condition, notes FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["status"] == "maintenance"
    # Condition follows status into maintenance - leaving the old condition
    # (often still "functional") in place read as a contradiction elsewhere
    # in the UI (e.g. Inventory's Condition column right next to a
    # Maintenance status pill).
    assert row["condition"] == "for_repair"
    assert "chuck slipping" in row["notes"]


def test_mark_for_maintenance_rejects_already_in_maintenance(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    set_maintenance(conn, asset_id)
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-maintenance", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_mark_for_maintenance_rejects_borrowed(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET status = 'borrowed' WHERE id = ?", (asset_id,))
    conn.commit()
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-maintenance", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409


def test_mark_for_maintenance_rejects_consumable(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    consumable_id = make_asset(conn, category_id, asset_type="consumable", quantity=10)
    resp = client.post(f"/api/v1/assets/{consumable_id}/mark-maintenance", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_mark_for_maintenance_forbidden_for_tool_keeper(as_tool_keeper, asset_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-maintenance", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_mark_for_maintenance_logs_action(as_admin, conn, asset_id):
    client, csrf, staff_id = as_admin
    client.post(f"/api/v1/assets/{asset_id}/mark-maintenance", json={}, headers={"X-CSRF-Token": csrf})
    log = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'marked_for_maintenance' AND target_id = ?", (asset_id,)
    ).fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id


# # # DEFAULT next_maintenance_due -------------------------------------------


def test_new_serialized_asset_gets_default_maintenance_due(conn, category_id):
    asset_id = make_asset(conn, category_id, name="No dates set")
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["next_maintenance_due"] is not None
    expected = (date.today() + timedelta(days=180)).isoformat()
    assert row["next_maintenance_due"] == expected


def test_new_asset_default_maintenance_due_anchors_to_last_calibrated(conn, category_id):
    calibrated = (date.today() - timedelta(days=30)).isoformat()
    asset_id = make_asset(conn, category_id, last_calibrated=calibrated)
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    expected = (date.today() - timedelta(days=30) + timedelta(days=180)).isoformat()
    assert row["next_maintenance_due"] == expected


def test_explicit_next_maintenance_due_is_not_overridden(conn, category_id):
    explicit = (date.today() + timedelta(days=5)).isoformat()
    asset_id = make_asset(conn, category_id, next_maintenance_due=explicit)
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["next_maintenance_due"] == explicit


def test_consumable_gets_no_default_maintenance_due(conn, category_id):
    asset_id = make_asset(conn, category_id, asset_type="consumable", quantity=10)
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["next_maintenance_due"] is None


def test_resolve_maintenance_blank_due_auto_recomputes_instead_of_clearing(conn, asset_id):
    set_maintenance_direct(conn, asset_id)
    result, error = resolve_maintenance(conn, asset_id, "functional", None, staff_id=None)
    assert error is None, error
    row = conn.execute("SELECT next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["next_maintenance_due"] is not None
    expected = (date.today() + timedelta(days=180)).isoformat()
    assert row["next_maintenance_due"] == expected


def set_maintenance_direct(conn, asset_id):
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset_id,))
    conn.commit()
