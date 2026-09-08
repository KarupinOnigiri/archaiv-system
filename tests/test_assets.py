"""Asset CRUD, soft-delete/undo/restore, bulk operations, categories, and
role gating - routes/assets.py + services/asset_service.py."""

from tests.conftest import TEST_PASSWORD, make_asset
from services import asset_service


# # # READ ---------------------------------------------------------------


def test_get_asset_success(as_admin, asset_id):
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/assets/{asset_id}")
    assert resp.status_code == 200
    assert resp.get_json()["id"] == asset_id


def test_get_asset_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/assets/999999")
    assert resp.status_code == 404


# # # BARCODE/RFID LOOKUP (decimal-scan fallback) ---------------------------
#
# Some readers set to type scans as keyboard input only type the decimal
# value of an EPC's last 3 bytes, not the full hex ID - see
# utils/validators.py's decimal_scan_hex_suffix() docstring for the real
# example this is derived from (EPC ...ABCDE12345, scanned as "14754629").


def test_get_asset_by_barcode_matches_full_hex_rfid(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, rfid_id="45494D00E00C00ABCDE12345")
    resp = client.get("/api/v1/assets/barcode/45494D00E00C00ABCDE12345")
    assert resp.status_code == 200


def test_get_asset_by_barcode_matches_decimal_scan_of_rfid_tail(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, rfid_id="45494D00E00C00ABCDE12345")
    resp = client.get("/api/v1/assets/barcode/14754629")
    assert resp.status_code == 200


def test_get_asset_by_barcode_decimal_scan_no_match_is_404(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, rfid_id="45494D00E00C00ABCDE12345")
    resp = client.get("/api/v1/assets/barcode/11111111")
    assert resp.status_code == 404


def test_asset_by_barcode_search_box_matches_decimal_scan(as_admin, conn, category_id):
    """Same fallback via the search box's Enter-key route (htmx, always 200)."""
    client, csrf, _ = as_admin
    asset_id = make_asset(conn, category_id, rfid_id="45494D00E00C00ABCDE12345")
    resp = client.get("/api/v1/assets/by-barcode/14754629")
    assert resp.status_code == 200
    assert str(asset_id) in resp.get_data(as_text=True)


def test_search_with_literal_percent_does_not_match_everything(conn, category_id):
    """A search term containing a literal '%' must be treated as a literal
    character, not a SQL LIKE wildcard - regression test for the ESCAPE
    clause added to get_assets_table_context's search query."""
    make_asset(conn, category_id, name="Drill 50% Off")
    make_asset(conn, category_id, name="Impact Wrench")

    results, error = asset_service.get_assets_table_context(
        conn, {"q": "50%"}, limit=20, offset=0
    )
    assert error is None
    names = [a["name"] for a in results["assets"]]
    assert "Drill 50% Off" in names
    assert "Impact Wrench" not in names


# # # CREATE --------------------------------------------------------------


def test_add_asset_success(as_admin, category_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/assets/add",
        json={"name": "Angle Grinder", "category_id": category_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["asset_id"].startswith("AST")


def test_add_asset_missing_name_is_rejected(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/assets/add", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.get_json().get("error", "").startswith("Missing required field")


def test_add_asset_forbidden_for_tool_keeper(as_tool_keeper, category_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/assets/add",
        json={"name": "Angle Grinder", "category_id": category_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_add_asset_allowed_for_in_charge(as_in_charge, category_id):
    client, csrf, _ = as_in_charge
    resp = client.post(
        "/api/v1/assets/add",
        json={"name": "Bench Vise", "category_id": category_id},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201


# # # UPDATE --------------------------------------------------------------


def test_edit_asset_success(as_admin, asset_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/edit",
        json={"name": "Renamed Drill", "condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200


def test_edit_asset_invalid_condition_is_rejected(as_admin, asset_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/edit",
        json={"condition": "not-a-real-condition"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_edit_asset_forbidden_for_tool_keeper(as_tool_keeper, asset_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        f"/api/v1/assets/{asset_id}/edit",
        json={"name": "Hacked Name"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_edit_asset_can_correct_serial_code(as_admin, conn, asset_id):
    """Used to be locked after creation - a data-entry typo was
    uncorrectable without deleting and recreating the asset."""
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/edit",
        json={"serial_code": "SN-CORRECTED-001"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert conn.execute("SELECT serial_code FROM assets WHERE id = ?", (asset_id,)).fetchone()["serial_code"] == (
        "SN-CORRECTED-001"
    )


def test_edit_asset_serial_code_duplicate_is_rejected(as_admin, conn, category_id, asset_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, serial_code="SN-TAKEN-001")
    resp = client.post(
        f"/api/v1/assets/{asset_id}/edit",
        json={"serial_code": "SN-TAKEN-001"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# # # DELETE / UNDO / RESTORE -----------------------------------------------


def test_delete_then_undo_delete(as_admin, conn, asset_id):
    client, csrf, _ = as_admin

    resp = client.post(
        f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    deleted_at = conn.execute(
        "SELECT deleted_at FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()["deleted_at"]
    assert deleted_at is not None

    resp = client.post(
        f"/api/v1/assets/{asset_id}/undo-delete", headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    deleted_at = conn.execute(
        "SELECT deleted_at FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()["deleted_at"]
    assert deleted_at is None


def test_delete_asset_forbidden_for_tool_keeper(as_tool_keeper, asset_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_restore_requires_correct_password(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/assets/{asset_id}/restore",
        json={"current_password": "wrong-password", "reason": "test restore"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401


def test_restore_requires_a_reason(as_admin, asset_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/assets/{asset_id}/restore",
        json={"current_password": TEST_PASSWORD, "reason": ""},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_restore_success_with_password_and_reason(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/assets/{asset_id}/restore",
        json={
            "current_password": TEST_PASSWORD,
            "reason": "returned to service",
            "condition": "functional",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT deleted_at, status FROM assets WHERE id = ?", (asset_id,)
    ).fetchone()
    assert row["deleted_at"] is None
    assert row["status"] == "available"


def test_restore_rejects_disposed_condition(as_admin, asset_id):
    """A restored asset can't be re-restored as 'disposed' - that's what delete is for."""
    client, csrf, _ = as_admin
    client.post(f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/assets/{asset_id}/restore",
        json={"current_password": TEST_PASSWORD, "reason": "test", "condition": "disposed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_restore_forbidden_for_in_charge(as_in_charge, conn, category_id):
    """restore_asset is admin-only, deliberately tighter than delete
    (admin/in_charge) - reversing a disposal is a bigger call."""
    client, csrf, _ = as_in_charge
    asset_id = make_asset(conn, category_id)
    client.post(f"/api/v1/assets/{asset_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/assets/{asset_id}/restore",
        json={"current_password": TEST_PASSWORD, "reason": "test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


# # # BULK OPERATIONS ---------------------------------------------------


def test_bulk_delete_skips_bad_ids_without_failing_the_batch(as_admin, conn):
    client, csrf, _ = as_admin
    good_id = make_asset(conn)

    resp = client.post(
        "/api/v1/assets/bulk-delete",
        json={"ids": [good_id, 999999]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert good_id in body.get("deleted", []) or body.get("deleted_count", 0) >= 1
    deleted_at = conn.execute(
        "SELECT deleted_at FROM assets WHERE id = ?", (good_id,)
    ).fetchone()["deleted_at"]
    assert deleted_at is not None


def test_bulk_restore_is_admin_only(as_in_charge, conn):
    client, csrf, _ = as_in_charge
    resp = client.post(
        "/api/v1/assets/bulk-restore", json={"ids": [1]}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_bulk_reassign_category_success(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    other_category = asset_service.add_category(conn, "Other Category", staff_id=None)[0]["id"]
    a1, a2 = make_asset(conn, category_id), make_asset(conn, category_id)

    resp = client.post(
        "/api/v1/assets/bulk-reassign-category",
        json={"ids": [a1, a2], "category_id": other_category},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    assert body["reassigned_count"] == 2
    for aid in (a1, a2):
        row = conn.execute("SELECT category_id FROM assets WHERE id = ?", (aid,)).fetchone()
        assert row["category_id"] == other_category


def test_bulk_reassign_category_rejects_invalid_category(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    a1 = make_asset(conn, category_id)
    resp = client.post(
        "/api/v1/assets/bulk-reassign-category",
        json={"ids": [a1], "category_id": 999999},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_bulk_reassign_category_skips_archived(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    other_category = asset_service.add_category(conn, "Other Category 2", staff_id=None)[0]["id"]
    a1 = make_asset(conn, category_id)
    conn.execute("UPDATE assets SET deleted_at = datetime('now') WHERE id = ?", (a1,))
    conn.commit()

    resp = client.post(
        "/api/v1/assets/bulk-reassign-category",
        json={"ids": [a1], "category_id": other_category},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["reassigned_count"] == 0
    assert len(body["skipped"]) == 1


def test_bulk_reassign_category_logs_action(as_admin, conn, category_id):
    client, csrf, staff_id = as_admin
    other_category = asset_service.add_category(conn, "Other Category 3", staff_id=None)[0]["id"]
    a1 = make_asset(conn, category_id)
    client.post(
        "/api/v1/assets/bulk-reassign-category",
        json={"ids": [a1], "category_id": other_category},
        headers={"X-CSRF-Token": csrf},
    )
    log = conn.execute(
        "SELECT * FROM audit_log WHERE action = 'bulk_reassigned_category'"
    ).fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id


def test_bulk_change_status_success(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    a1 = make_asset(conn, category_id)
    resp = client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [a1], "status": "maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["changed_count"] == 1
    row = conn.execute("SELECT status FROM assets WHERE id = ?", (a1,)).fetchone()
    assert row["status"] == "maintenance"


def test_bulk_change_status_rejects_invalid_status(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    a1 = make_asset(conn, category_id)
    resp = client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [a1], "status": "borrowed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_bulk_change_status_skips_borrowed(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    a1 = make_asset(conn, category_id)
    conn.execute("UPDATE assets SET status = 'borrowed' WHERE id = ?", (a1,))
    conn.commit()

    resp = client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [a1], "status": "maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["changed_count"] == 0
    assert len(body["skipped"]) == 1


def test_bulk_change_status_skips_consumables(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    consumable_id = make_asset(conn, category_id, asset_type="consumable", quantity=5)
    resp = client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [consumable_id], "status": "maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["changed_count"] == 0
    assert len(body["skipped"]) == 1


def test_bulk_change_status_forbidden_for_tool_keeper(as_tool_keeper, conn, category_id):
    client, csrf, _ = as_tool_keeper
    a1 = make_asset(conn, category_id)
    resp = client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [a1], "status": "maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_bulk_change_status_logs_action(as_admin, conn, category_id):
    client, csrf, staff_id = as_admin
    a1 = make_asset(conn, category_id)
    client.post(
        "/api/v1/assets/bulk-change-status",
        json={"ids": [a1], "status": "maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    log = conn.execute("SELECT * FROM audit_log WHERE action = 'bulk_changed_status'").fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id


def test_bulk_restore_success(as_admin, conn):
    client, csrf, _ = as_admin
    asset_a = make_asset(conn)
    client.post(f"/api/v1/assets/{asset_a}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        "/api/v1/assets/bulk-restore", json={"ids": [asset_a]}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    deleted_at = conn.execute(
        "SELECT deleted_at FROM assets WHERE id = ?", (asset_a,)
    ).fetchone()["deleted_at"]
    assert deleted_at is None


# # # CATEGORIES ----------------------------------------------------------


def test_add_category_success(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/categories/add", json={"name": "Hand Tools"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 201


def test_add_duplicate_category_is_rejected(as_admin, category_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/categories/add", json={"name": "Power Tools"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_add_category_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/categories/add", json={"name": "Should Not Work"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_delete_category_success(as_admin, category_id):
    client, csrf, _ = as_admin
    resp = client.post(f"/api/v1/categories/{category_id}/delete", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


# # # RESOLVE MAINTENANCE (quick action, admin/in_charge) -------------------


def set_maintenance(conn, asset_id):
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset_id,))
    conn.commit()


def test_resolve_maintenance_success(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()

    row = conn.execute("SELECT status, condition FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["status"] == "available"
    assert row["condition"] == "functional"


def test_resolve_maintenance_appends_a_timestamped_note(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    set_maintenance(conn, asset_id)
    client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional", "notes": "replaced worn brushes"},
        headers={"X-CSRF-Token": csrf},
    )
    notes = conn.execute("SELECT notes FROM assets WHERE id = ?", (asset_id,)).fetchone()["notes"]
    assert "replaced worn brushes" in notes
    assert "Maintenance resolved" in notes


def test_resolve_maintenance_rejects_asset_not_in_maintenance(as_admin, asset_id):
    """asset_id fixture defaults to 'available' - resolving it is a no-op error, not silently allowed."""
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_resolve_maintenance_rejects_invalid_condition(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "not-a-real-condition"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_resolve_maintenance_forbidden_for_tool_keeper(as_tool_keeper, conn, asset_id):
    client, csrf, _ = as_tool_keeper
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_resolve_maintenance_allowed_for_in_charge(as_in_charge, conn, asset_id):
    client, csrf, _ = as_in_charge
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200


def test_resolve_maintenance_form_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/assets/999999/resolve-maintenance-form")
    assert resp.status_code == 404


def test_resolve_maintenance_functional_updates_last_calibrated_to_today(as_admin, conn, asset_id):
    """Marking an asset fixed/good out of maintenance counts as a fresh
    calibration - services/asset_service.py's resolve_maintenance."""
    import datetime

    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET last_calibrated = '2020-01-01' WHERE id = ?", (asset_id,))
    conn.commit()
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    row = conn.execute("SELECT last_calibrated FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["last_calibrated"] == datetime.date.today().isoformat()


def test_resolve_maintenance_non_functional_leaves_last_calibrated_untouched(as_admin, conn, asset_id):
    """Only actually fixed (condition='functional') counts as calibrated -
    for_repair/inoperable means it's still not okay."""
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET last_calibrated = '2020-01-01' WHERE id = ?", (asset_id,))
    conn.commit()
    set_maintenance(conn, asset_id)
    resp = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "for_repair"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    row = conn.execute("SELECT last_calibrated FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["last_calibrated"] == "2020-01-01"


# # # MARK CALIBRATED (quick action, admin/in_charge) -----------------------


def test_mark_calibrated_success(as_admin, conn, asset_id):
    import datetime

    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET last_calibrated = '2020-01-01', next_maintenance_due = NULL WHERE id = ?", (asset_id,))
    conn.commit()
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-calibrated", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200, resp.get_json()

    row = conn.execute("SELECT last_calibrated, next_maintenance_due FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["last_calibrated"] == datetime.date.today().isoformat()
    assert row["next_maintenance_due"] is not None
    assert row["next_maintenance_due"] > row["last_calibrated"]


def test_mark_calibrated_logs_action(as_admin, conn, asset_id):
    client, csrf, staff_id = as_admin
    client.post(f"/api/v1/assets/{asset_id}/mark-calibrated", json={}, headers={"X-CSRF-Token": csrf})
    log = conn.execute("SELECT * FROM audit_log WHERE action = 'marked_calibrated'").fetchone()
    assert log is not None
    assert log["staff_id"] == staff_id


def test_mark_calibrated_rejects_consumable(as_admin, conn, category_id):
    from tests.conftest import make_asset

    client, csrf, _ = as_admin
    consumable_id = make_asset(conn, category_id, asset_type="consumable", quantity=10)
    resp = client.post(f"/api/v1/assets/{consumable_id}/mark-calibrated", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_mark_calibrated_forbidden_for_tool_keeper(as_tool_keeper, asset_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-calibrated", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_mark_calibrated_allowed_for_in_charge(as_in_charge, asset_id):
    client, csrf, _ = as_in_charge
    resp = client.post(f"/api/v1/assets/{asset_id}/mark-calibrated", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


def test_mark_calibrated_form_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/assets/999999/mark-calibrated-form")
    assert resp.status_code == 404


# # # RELEASE RESERVATION (admin-only bypass for an abandoned checkout) ------


def reserve_via_abandoned_cart(conn, member_id, asset_id, staff_id=None):
    """Simulates a tool_keeper who scanned an asset into a cart and then
    logged out (or crashed) without cancelling or completing - an open
    checkout_sessions/checkout_session_items pair with no corresponding
    transaction, and the asset left in 'reserved'."""
    cursor = conn.execute(
        "INSERT INTO checkout_sessions (member_id, status, staff_id) VALUES (?, 'open', ?)",
        (member_id, staff_id),
    )
    session_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO checkout_session_items (checkout_session_id, asset_id) VALUES (?, ?)",
        (session_id, asset_id),
    )
    conn.execute("UPDATE assets SET status = 'reserved' WHERE id = ?", (asset_id,))
    conn.commit()
    return session_id


def test_release_reservation_success(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    session_id = reserve_via_abandoned_cart(conn, member_id, asset_id)

    resp = client.post(f"/api/v1/assets/{asset_id}/release-reservation", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200, resp.get_json()

    asset = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["status"] == "available"

    remaining_items = conn.execute(
        "SELECT COUNT(*) AS c FROM checkout_session_items WHERE checkout_session_id = ?", (session_id,)
    ).fetchone()["c"]
    assert remaining_items == 0

    # The abandoned session itself is left alone - not force-cancelled --
    # see services/transaction_service.release_reserved_asset's docstring.
    session_status = conn.execute(
        "SELECT status FROM checkout_sessions WHERE id = ?", (session_id,)
    ).fetchone()["status"]
    assert session_status == "open"


def test_release_reservation_rejects_asset_not_reserved(as_admin, asset_id):
    """json=... (not a plain form POST) to take the route's JSON error
    branch, which returns the real status code - the htmx/form branch
    deliberately always answers 200 with an inline error message instead
    (same as resolve-maintenance's error responder), since it's meant to
    be swapped into the DOM, not treated as a failed request."""
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/assets/{asset_id}/release-reservation", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 409
    assert "not currently reserved" in resp.get_json()["error"]


def test_release_reservation_forbidden_for_in_charge(as_in_charge, conn, member_id, asset_id):
    """Admin-only, deliberately tighter than Resolve Maintenance - this
    bypasses another staff member's in-progress cart."""
    client, csrf, _ = as_in_charge
    reserve_via_abandoned_cart(conn, member_id, asset_id)
    resp = client.post(f"/api/v1/assets/{asset_id}/release-reservation", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_release_reservation_forbidden_for_tool_keeper(as_tool_keeper, conn, member_id, asset_id):
    client, csrf, _ = as_tool_keeper
    reserve_via_abandoned_cart(conn, member_id, asset_id)
    resp = client.post(f"/api/v1/assets/{asset_id}/release-reservation", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_release_reservation_form_shows_who_is_holding_it(as_admin, conn, member_id, asset_id):
    client, csrf, admin_staff_id = as_admin
    reserve_via_abandoned_cart(conn, member_id, asset_id, staff_id=admin_staff_id)
    resp = client.get(f"/api/v1/assets/{asset_id}/release-reservation-form")
    assert resp.status_code == 200
    member_name = conn.execute("SELECT first_name FROM members WHERE id = ?", (member_id,)).fetchone()[0]
    assert member_name.encode() in resp.data


def test_release_reservation_form_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/assets/999999/release-reservation-form")
    assert resp.status_code == 404


# # # CSV IMPORT (admin/in_charge) -------------------------------------------


def csv_upload(client, csrf, content, mode="commit", filename="assets.csv"):
    import io

    return client.post(
        "/api/v1/assets/import",
        data={"file": (io.BytesIO(content.encode("utf-8")), filename), "mode": mode},
        headers={"X-CSRF-Token": csrf},
        content_type="multipart/form-data",
    )


def test_import_form_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/assets/import-form")
    assert resp.status_code == 403


def test_import_requires_a_file(as_admin):
    """Non-JSON/htmx path always answers 200 with an inline error (HX-Retarget
    swaps it into the modal) - see routes/assets.py's _import_error_response."""
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/assets/import", data={}, headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#modal-container"
    assert b"Missing file upload" in resp.data


def test_import_rejects_missing_required_columns(as_admin):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "category\nPower Tools\n")
    assert resp.status_code == 200
    assert b"missing required column" in resp.data.lower()


def test_import_preview_does_not_persist(as_admin, conn):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "name\nPreview Drill\n", mode="preview")
    assert resp.status_code == 200
    assert conn.execute("SELECT id FROM assets WHERE name = 'Preview Drill'").fetchone() is None


def test_import_commit_creates_assets(as_admin, conn):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "name\nCommitted Drill\n", mode="commit")
    assert resp.status_code == 200
    assert conn.execute("SELECT id FROM assets WHERE name = 'Committed Drill'").fetchone() is not None


def test_import_creates_a_new_category_if_it_does_not_exist(as_admin, conn):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "name,category\nCat Drill,Totally New Category\n", mode="commit")
    assert resp.status_code == 200
    assert conn.execute("SELECT id FROM categories WHERE name = 'Totally New Category'").fetchone() is not None


def test_import_archives_a_row_with_deleted_at_set(as_admin, conn):
    """Re-importing our own export (which includes deleted_at for archived
    rows) round-trips an asset straight into the archive, not available."""
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "name,deleted_at\nArchived Drill,2026-01-01\n", mode="commit")
    assert resp.status_code == 200
    row = conn.execute("SELECT deleted_at FROM assets WHERE name = 'Archived Drill'").fetchone()
    assert row["deleted_at"] is not None


def test_import_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = csv_upload(client, csrf, "name\nShould Not Import\n")
    assert resp.status_code == 403


def test_import_rejects_invalid_mode(as_admin):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "name\nX\n", mode="not-a-real-mode")
    assert resp.status_code == 200
    assert b"mode must be" in resp.data.lower()
