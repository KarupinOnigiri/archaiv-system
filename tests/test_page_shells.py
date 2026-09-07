"""Full-page routes that are mostly a thin shell around already-tested
API endpoints - routes/pages.py's barcodes_page/requests_page/reports_page.
Monitoring's page shell (routes/pages.py's monitoring_page) is already
covered in tests/test_monitoring.py; not duplicated here."""

from tests.conftest import make_asset, make_member


# # # /barcodes (admin/in_charge only) --------------------------------------


def test_barcodes_requires_login(client):
    resp = client.get("/barcodes", follow_redirects=False)
    assert resp.status_code == 302


def test_barcodes_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/barcodes")
    assert resp.status_code == 403


def test_barcodes_allowed_for_in_charge(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/barcodes")
    assert resp.status_code == 200


def test_barcodes_lists_only_present_assets_and_active_members(as_admin, conn, department_id, category_id):
    client, csrf, _ = as_admin

    visible_asset = make_asset(conn, category_id, name="Visible Drill")
    deleted_asset = make_asset(conn, category_id, name="Deleted Drill")
    conn.execute(
        "UPDATE assets SET deleted_at = datetime('now', 'localtime') WHERE id = ?", (deleted_asset,)
    )

    visible_member = make_member(conn, department_id, first_name="VisibleMember")
    inactive_member = make_member(conn, department_id, first_name="InactiveMember")
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (inactive_member,))
    conn.commit()

    resp = client.get("/barcodes")
    assert resp.status_code == 200
    assert b"Visible Drill" in resp.data
    assert b"Deleted Drill" not in resp.data
    assert b"VisibleMember" in resp.data
    assert b"InactiveMember" not in resp.data


# # # /requests (Approvals queue shell) --------------------------------------


def test_requests_page_requires_login(client):
    resp = client.get("/requests", follow_redirects=False)
    assert resp.status_code == 302


def test_requests_page_loads_for_admin(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/requests")
    assert resp.status_code == 200


def test_requests_page_loads_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/requests")
    assert resp.status_code == 200


def test_requests_page_blocked_when_feature_disabled(as_admin, conn):
    client, csrf, _ = as_admin
    conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'feature_requests_enabled'")
    conn.commit()
    resp = client.get("/requests", follow_redirects=False)
    assert resp.status_code == 302


def test_requests_page_defaults_to_unrestriction_tab_pending_status(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/requests")
    assert resp.status_code == 200


def test_requests_page_invalid_tab_falls_back_to_unrestriction(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/requests?tab=not-a-real-tab")
    assert resp.status_code == 200  # falls back rather than erroring


def test_requests_page_deactivation_tab(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/requests?tab=deactivation&d_status=approved")
    assert resp.status_code == 200


# # # /reports (page shell - numbers come from stats.py's JSON endpoints) ----


def test_reports_page_requires_login(client):
    resp = client.get("/reports", follow_redirects=False)
    assert resp.status_code == 302


def test_reports_page_loads(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/reports")
    assert resp.status_code == 200


def test_reports_page_blocked_when_feature_disabled(as_admin, conn):
    client, csrf, _ = as_admin
    conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'feature_reports_enabled'")
    conn.commit()
    resp = client.get("/reports", follow_redirects=False)
    assert resp.status_code == 302


def test_reports_page_sanitizes_invalid_since(as_admin):
    """_report_date drops (doesn't error on) an invalid since/until in the
    query string - it's just pre-filling a date picker, not a hard filter."""
    client, csrf, _ = as_admin
    resp = client.get("/reports?since=not-a-date")
    assert resp.status_code == 200


def test_reports_page_keeps_a_valid_since(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/reports?since=2026-01-01")
    assert resp.status_code == 200
    assert b"2026-01-01" in resp.data
