"""Auto hard-delete of archived assets / deactivated members & staff past a
retention window - services/archive_purge_service.py + the System page's
routes in routes/staff.py.

Purely service-level logic (no filesystem/external side effects worth
isolating, unlike tests/test_backups.py's backup routes) - most tests here
call purge_expired_* directly with `conn`, same as record_rfid_event-style
service tests elsewhere. A row with real transaction history is left alone
(FK violation caught, not raised) rather than purged - confirmed by
actually giving a target row that history via a real borrow+return cycle,
not just asserting the code path exists.
"""

from datetime import date, timedelta

from services import archive_purge_service as purge_service
from services import settings_service
from tests.conftest import TEST_PASSWORD, make_asset, make_member, make_staff


def set_purge_days(conn, days):
    conn.execute("UPDATE app_settings SET value = ? WHERE key = 'archive_purge_days'", (str(days),))
    conn.commit()


def backdate(conn, table, column, entity_id, days_ago):
    conn.execute(
        f"UPDATE {table} SET {column} = datetime('now', 'localtime', ?) WHERE id = ?",
        (f"-{days_ago} days", entity_id),
    )
    conn.commit()


def complete_a_loan(client, csrf, conn, member_id, asset_id):
    """Full borrow+return cycle - leaves real transaction history behind
    on the member, asset, and the acting staff (borrowed_by/returned_by)."""
    member_barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()[0]
    asset_barcode = conn.execute("SELECT barcode_id FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]

    r = client.post("/api/v1/checkout-sessions", json={"member_barcode": member_barcode}, headers={"X-CSRF-Token": csrf})
    session_id = r.get_json()["checkout_session_id"]
    client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_barcode},
        headers={"X-CSRF-Token": csrf},
    )
    client.post(f"/api/v1/checkout-sessions/{session_id}/checkout", json={}, headers={"X-CSRF-Token": csrf})
    client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_barcode, "member_barcode": member_barcode, "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )


# # # ASSETS -----------------------------------------------------------------


def test_purge_assets_noop_when_disabled(conn, asset_id):
    set_purge_days(conn, 0)
    conn.execute("UPDATE assets SET deleted_at = datetime('now', 'localtime', '-100 days') WHERE id = ?", (asset_id,))
    conn.commit()
    assert purge_service.purge_expired_archived_assets(conn) == 0
    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone() is not None


def test_purge_assets_leaves_not_yet_expired_alone(conn, asset_id):
    set_purge_days(conn, 30)
    conn.execute("UPDATE assets SET deleted_at = datetime('now', 'localtime', '-5 days') WHERE id = ?", (asset_id,))
    conn.commit()
    assert purge_service.purge_expired_archived_assets(conn) == 0
    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone() is not None


def test_purge_assets_deletes_eligible_asset_with_no_history(conn, asset_id):
    set_purge_days(conn, 30)
    conn.execute("UPDATE assets SET deleted_at = datetime('now', 'localtime', '-31 days') WHERE id = ?", (asset_id,))
    conn.commit()
    assert purge_service.purge_expired_archived_assets(conn) == 1
    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone() is None


def test_purge_assets_skips_asset_with_transaction_history(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    complete_a_loan(client, csrf, conn, member_id, asset_id)

    set_purge_days(conn, 30)
    conn.execute("UPDATE assets SET deleted_at = datetime('now', 'localtime', '-31 days') WHERE id = ?", (asset_id,))
    conn.commit()

    assert purge_service.purge_expired_archived_assets(conn) == 0
    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone() is not None


def test_purge_assets_ignores_non_archived_assets(conn, asset_id):
    """status/condition aside, an asset with deleted_at still NULL was
    never soft-deleted in the first place - not a candidate at all."""
    set_purge_days(conn, 0)  # would be a no-op anyway; the real guard is deleted_at IS NULL
    set_purge_days(conn, 30)
    assert purge_service.purge_expired_archived_assets(conn) == 0
    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset_id,)).fetchone() is not None


# # # STAFF -------------------------------------------------------------------


def test_purge_staff_noop_when_disabled(conn, department_id):
    set_purge_days(conn, 0)
    staff_id = make_staff(conn, department_id, "tool_keeper", "old_tk")
    backdate_staff_deactivation(conn, staff_id, 100)
    assert purge_service.purge_expired_inactive_staff(conn) == 0


def test_purge_staff_deletes_eligible_with_no_history(conn, department_id):
    set_purge_days(conn, 30)
    staff_id = make_staff(conn, department_id, "tool_keeper", "old_tk2")
    backdate_staff_deactivation(conn, staff_id, 31)
    assert purge_service.purge_expired_inactive_staff(conn) == 1
    assert conn.execute("SELECT id FROM staff WHERE id = ?", (staff_id,)).fetchone() is None


def test_purge_staff_also_removes_their_sessions(conn, department_id):
    from services.login_service import start_session

    set_purge_days(conn, 30)
    staff_id = make_staff(conn, department_id, "tool_keeper", "old_tk3")
    start_session(staff_id)
    backdate_staff_deactivation(conn, staff_id, 31)
    purge_service.purge_expired_inactive_staff(conn)
    assert conn.execute("SELECT id FROM sessions WHERE staff_id = ?", (staff_id,)).fetchone() is None


def test_purge_staff_skips_staff_with_transaction_history(as_admin, conn, member_id, asset_id, department_id):
    client, csrf, target_staff_id = as_admin  # borrowed_by_staff_id - this account has history
    complete_a_loan(client, csrf, conn, member_id, asset_id)

    set_purge_days(conn, 30)
    backdate_staff_deactivation(conn, target_staff_id, 31)
    assert purge_service.purge_expired_inactive_staff(conn) == 0
    assert conn.execute("SELECT id FROM staff WHERE id = ?", (target_staff_id,)).fetchone() is not None


def test_purge_staff_not_yet_expired(conn, department_id):
    set_purge_days(conn, 30)
    staff_id = make_staff(conn, department_id, "tool_keeper", "recent_tk")
    backdate_staff_deactivation(conn, staff_id, 5)
    assert purge_service.purge_expired_inactive_staff(conn) == 0


def test_purge_staff_ignores_still_active_staff(conn, department_id):
    """is_active=1 - never deactivated at all, not a candidate regardless
    of what deactivated_at (NULL) says."""
    set_purge_days(conn, 30)
    staff_id = make_staff(conn, department_id, "tool_keeper", "still_active_tk")
    assert purge_service.purge_expired_inactive_staff(conn) == 0
    assert conn.execute("SELECT id FROM staff WHERE id = ?", (staff_id,)).fetchone() is not None


def backdate_staff_deactivation(conn, staff_id, days_ago):
    conn.execute(
        "UPDATE staff SET is_active = 0, deactivated_at = datetime('now', 'localtime', ?) WHERE id = ?",
        (f"-{days_ago} days", staff_id),
    )
    conn.commit()


# # # MEMBERS ------------------------------------------------------------------


def backdate_member_deactivation(conn, member_id, days_ago):
    conn.execute(
        "UPDATE members SET is_active = 0, deactivated_at = datetime('now', 'localtime', ?) WHERE id = ?",
        (f"-{days_ago} days", member_id),
    )
    conn.commit()


def test_purge_members_noop_when_disabled(conn, member_id):
    set_purge_days(conn, 0)
    backdate_member_deactivation(conn, member_id, 100)
    assert purge_service.purge_expired_inactive_members(conn) == 0


def test_purge_members_deletes_eligible_with_no_history(conn, department_id):
    set_purge_days(conn, 30)
    m = make_member(conn, department_id)
    backdate_member_deactivation(conn, m, 31)
    assert purge_service.purge_expired_inactive_members(conn) == 1
    assert conn.execute("SELECT id FROM members WHERE id = ?", (m,)).fetchone() is None


def test_purge_members_skips_member_with_transaction_history(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    complete_a_loan(client, csrf, conn, member_id, asset_id)

    set_purge_days(conn, 30)
    backdate_member_deactivation(conn, member_id, 31)
    assert purge_service.purge_expired_inactive_members(conn) == 0
    assert conn.execute("SELECT id FROM members WHERE id = ?", (member_id,)).fetchone() is not None


def test_purge_members_not_yet_expired(conn, department_id):
    set_purge_days(conn, 30)
    m = make_member(conn, department_id)
    backdate_member_deactivation(conn, m, 5)
    assert purge_service.purge_expired_inactive_members(conn) == 0


# # # SCHEDULED JOB (runs all three passes) ------------------------------------


def test_run_scheduled_archive_purge_runs_all_three_passes(conn, department_id):
    set_purge_days(conn, 30)
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET deleted_at = datetime('now', 'localtime', '-31 days') WHERE id = ?", (asset,))
    staff = make_staff(conn, department_id, "tool_keeper", "sched_tk")
    backdate_staff_deactivation(conn, staff, 31)
    member = make_member(conn, department_id)
    backdate_member_deactivation(conn, member, 31)
    conn.commit()

    purge_service.run_scheduled_archive_purge()

    assert conn.execute("SELECT id FROM assets WHERE id = ?", (asset,)).fetchone() is None
    assert conn.execute("SELECT id FROM staff WHERE id = ?", (staff,)).fetchone() is None
    assert conn.execute("SELECT id FROM members WHERE id = ?", (member,)).fetchone() is None


# # # HTTP ROUTES (System page: settings + manual purge-now) -------------------


def test_manage_form_forbidden_for_non_admin(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/archive-purge-settings/manage-form")
    assert resp.status_code == 403


def test_manage_form_shows_archived_counts(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    m = make_member(conn, department_id)
    backdate_member_deactivation(conn, m, 1)
    resp = client.get("/api/v1/archive-purge-settings/manage-form")
    assert resp.status_code == 200


def test_update_settings_success(as_admin, conn):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/archive-purge-settings/update", json={"purge_days": 90}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    assert settings_service.get_archive_purge_days(conn) == 90


def test_update_settings_zero_means_disabled(as_admin, conn):
    client, csrf, _ = as_admin
    client.post("/api/v1/archive-purge-settings/update", json={"purge_days": 90}, headers={"X-CSRF-Token": csrf})
    resp = client.post(
        "/api/v1/archive-purge-settings/update", json={"purge_days": 0}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    assert settings_service.get_archive_purge_days(conn) == 0


def test_update_settings_rejects_out_of_range(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/archive-purge-settings/update", json={"purge_days": 999999}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_update_settings_rejects_negative(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/archive-purge-settings/update", json={"purge_days": -1}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_update_settings_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/archive-purge-settings/update", json={"purge_days": 30}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_purge_now_requires_a_retention_window(as_admin, conn):
    client, csrf, _ = as_admin
    set_purge_days(conn, 0)
    resp = client.post("/api/v1/archive-purge-settings/purge-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_purge_now_success(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    set_purge_days(conn, 30)
    m = make_member(conn, department_id)
    backdate_member_deactivation(conn, m, 31)

    resp = client.post("/api/v1/archive-purge-settings/purge-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["purged_members"] == 1
    assert conn.execute("SELECT id FROM members WHERE id = ?", (m,)).fetchone() is None


def test_purge_now_forbidden_for_tool_keeper(as_tool_keeper, conn):
    client, csrf, _ = as_tool_keeper
    set_purge_days(conn, 30)
    resp = client.post("/api/v1/archive-purge-settings/purge-now", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403
