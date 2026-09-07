"""Reports tab: usage widgets, staff activity, "my activity" (tool_keeper's
own), customizable layout, and export - routes/stats.py +
services/stats_service.py + services/report_layout_service.py.

Widget endpoints (most/least-borrowed, loan-duration, etc.) are admin/
in_charge only; the "my/*" ones and layout are open to tool_keeper too
(scoped to their own staff_id); staff/activity is admin-only.
"""

from datetime import date, timedelta

from tests.conftest import TEST_PASSWORD, make_asset
from services import stats_service

WIDGET_ROUTES_ADMIN_IN_CHARGE = [
    "/api/v1/stats/assets/most-borrowed",
    "/api/v1/stats/assets/least-borrowed",
    "/api/v1/stats/categories/most-borrowed",
    "/api/v1/stats/loan-duration",
    "/api/v1/stats/members/top-borrowers",
    "/api/v1/stats/members/overdue-frequency",
    "/api/v1/stats/assets/condition-changes",
    "/api/v1/stats/summary",
    "/api/v1/stats/assets/valuation",
]

MY_ACTIVITY_ROUTES = [
    "/api/v1/stats/my/checkouts",
    "/api/v1/stats/my/returns",
    "/api/v1/stats/my/overdue-handled",
]


# # # FEATURE FLAG GATE --------------------------------------------------------


def test_blocked_when_reports_feature_disabled(as_admin):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/feature-flags/update",
        json={"current_password": TEST_PASSWORD, "flags": {"reports": False}},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.get("/api/v1/stats/summary")
    assert resp.status_code == 403


# # # WIDGETS (admin/in_charge only) -------------------------------------------


def test_widget_routes_return_200_on_empty_data(as_admin):
    client, csrf, _ = as_admin
    for route in WIDGET_ROUTES_ADMIN_IN_CHARGE:
        resp = client.get(route)
        assert resp.status_code == 200, f"{route} -> {resp.status_code}: {resp.get_json()}"


def test_widget_routes_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    for route in WIDGET_ROUTES_ADMIN_IN_CHARGE:
        resp = client.get(route)
        assert resp.status_code == 403, route


def test_widget_routes_allowed_for_in_charge(as_in_charge):
    client, csrf, _ = as_in_charge
    for route in WIDGET_ROUTES_ADMIN_IN_CHARGE:
        resp = client.get(route)
        assert resp.status_code == 200, route


def test_most_borrowed_assets_rejects_invalid_since(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/assets/most-borrowed?since=not-a-date")
    assert resp.status_code == 400


def test_most_borrowed_assets_rejects_invalid_limit(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/assets/most-borrowed?limit=notanumber")
    assert resp.status_code == 400


def test_most_borrowed_assets_reflects_a_real_loan(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    r = client.post(
        "/api/v1/checkout-sessions", json={"member_barcode": _barcode(conn, "members", member_id)},
        headers={"X-CSRF-Token": csrf},
    )
    session_id = r.get_json()["checkout_session_id"]
    client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": _barcode(conn, "assets", asset_id)},
        headers={"X-CSRF-Token": csrf},
    )
    client.post(f"/api/v1/checkout-sessions/{session_id}/checkout", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.get("/api/v1/stats/assets/most-borrowed")
    assert resp.status_code == 200
    results = resp.get_json().get("results") or resp.get_json()
    assert len(str(results)) > 0  # non-empty response; exact shape is stats_service's own concern
    assert results[0]["barcode_id"] == _barcode(conn, "assets", asset_id)


def _barcode(conn, table, entity_id):
    return conn.execute(f"SELECT barcode_id FROM {table} WHERE id = ?", (entity_id,)).fetchone()["barcode_id"]


# # # STAFF ACTIVITY (admin only) ----------------------------------------------


def test_staff_activity_admin_only(as_admin):
    admin_client, admin_csrf, _ = as_admin
    resp = admin_client.get("/api/v1/stats/staff/activity")
    assert resp.status_code == 200


def test_staff_activity_forbidden_for_in_charge(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/stats/staff/activity")
    assert resp.status_code == 403


def test_staff_activity_rejects_invalid_date(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/staff/activity?since=nope")
    assert resp.status_code == 400


def test_staff_activity_avg_session_reflects_login_session_not_checkout_cart(conn, admin_id):
    """Regression guard: avg_session_minutes used to be computed off
    checkout_sessions (the shopping-cart-style borrow session), which is
    unrelated and usually empty, leaving this column blank for most staff.
    It now reads the `sessions` table (actual login duration).

    Calls the service function directly rather than through an authenticated
    request - login_required's sliding-idle-timeout bumps last_active_at to
    "now" on every request (including the one fetching this very report),
    which would overwrite the manually-set value below before the query runs."""
    from services.login_service import start_session
    from services import stats_service

    token, _csrf = start_session(admin_id)
    conn.execute(
        "UPDATE sessions SET last_active_at = datetime(created_at, '+15 minutes') WHERE token = ?",
        (token,),
    )
    conn.commit()

    result = stats_service.staff_activity(conn, None, None)
    row = next(r for r in result["results"] if r["id"] == admin_id)
    assert row["avg_session_minutes"] == 15.0


# # # MY ACTIVITY (admin/in_charge/tool_keeper, scoped to self) ---------------


def test_my_activity_routes_available_to_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    for route in MY_ACTIVITY_ROUTES:
        resp = client.get(route)
        assert resp.status_code == 200, route


def test_my_checkouts_only_counts_this_staff_members_own(client, second_client, conn, department_id, member_id, asset_id):
    from tests.conftest import login_as, make_staff

    tk1 = make_staff(conn, department_id, "tool_keeper", "tk1")
    tk2 = make_staff(conn, department_id, "tool_keeper", "tk2")

    csrf1 = login_as(client, conn, tk1)
    r = client.post(
        "/api/v1/checkout-sessions", json={"member_barcode": _barcode(conn, "members", member_id)},
        headers={"X-CSRF-Token": csrf1},
    )
    session_id = r.get_json()["checkout_session_id"]
    client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": _barcode(conn, "assets", asset_id)},
        headers={"X-CSRF-Token": csrf1},
    )
    client.post(f"/api/v1/checkout-sessions/{session_id}/checkout", json={}, headers={"X-CSRF-Token": csrf1})

    csrf2 = login_as(second_client, conn, tk2)
    resp = second_client.get("/api/v1/stats/my/checkouts")
    assert resp.status_code == 200
    body = resp.get_json()
    total = body.get("total_count", body.get("total", 0))
    assert total == 0  # tk2 never checked anything out


# # # LAYOUT ---------------------------------------------------------------


def test_layout_get_returns_widgets_for_role(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/stats/layout")
    assert resp.status_code == 200
    body = resp.get_json()
    assert "layout" in body
    assert "widgets" in body
    # tool_keeper shouldn't see admin/in_charge-only widgets in their own list
    widget_ids = {w["id"] for w in body["widgets"]}
    assert "staff_activity" not in widget_ids


def test_layout_save_and_roundtrip(as_admin):
    """get_report_layout always returns the full, normalized layout (every
    widget_id for this role gets a position/visible entry, defaults filled
    in) - not a raw echo of whatever subset was POSTed - so this checks
    the one entry that was actually changed, not list equality."""
    client, csrf, _ = as_admin
    new_layout = [
        {"widget_id": "summary", "visible": True},
        {"widget_id": "most_borrowed_assets", "visible": False},
    ]
    resp = client.post("/api/v1/stats/layout", json={"layout": new_layout}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    resp2 = client.get("/api/v1/stats/layout")
    saved = {row["widget_id"]: row["visible"] for row in resp2.get_json()["layout"]}
    assert saved["most_borrowed_assets"] is False


def test_layout_save_rejects_malformed_body(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/stats/layout", json={"nope": "bad"}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_layout_reset(as_admin):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/stats/layout", json={"layout": [{"id": "summary", "visible": False}]}, headers={"X-CSRF-Token": csrf}
    )
    resp = client.post("/api/v1/stats/layout/reset", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


# # # EXPORT ----------------------------------------------------------------


def test_export_rejects_invalid_format(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?format=pdf")
    assert resp.status_code == 400


def test_export_rejects_invalid_date(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?since=not-a-date")
    assert resp.status_code == 400


def test_export_xlsx_default_all_sections(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    assert len(resp.data) > 0


def test_export_single_section_csv(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?sections=summary&format=csv")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")


def test_export_multiple_sections_csv_is_zipped(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?sections=summary,most_borrowed_assets&format=csv")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "application/zip"


def test_export_unknown_section_ids_are_silently_dropped_not_403(as_admin):
    """A tampered `sections` id is dropped, not rejected - matches the
    route's own docstring."""
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/stats/export?sections=not-a-real-widget-id")
    # Falls through to "no exportable sections selected" once the bogus id
    # is filtered out, rather than a 403/404 for the tampered id itself.
    assert resp.status_code == 400
    assert "No exportable sections" in resp.get_json()["error"]


def test_export_forbidden_widget_id_is_dropped_for_tool_keeper(as_tool_keeper):
    """staff_activity is admin-only - a tool_keeper requesting it by id
    shouldn't get it just because they know the id: it's a real, valid
    widget id (unlike test_export_unknown_section_ids_are_silently_dropped_
    not_403's made-up one), just not one this role is allowed."""
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/stats/export?sections=staff_activity&format=csv")
    assert resp.status_code == 400
    assert "No exportable sections" in resp.get_json()["error"]


# # # ASSET VALUATION ------------------------------------------------------


def test_asset_valuation_summary_none_when_no_asset_priced(conn, category_id):
    make_asset(conn, category_id, name="Unpriced Drill")
    assert stats_service.asset_valuation_summary(conn) is None


def test_asset_valuation_summary_totals(conn, category_id):
    make_asset(conn, category_id, name="Priced A", purchase_cost=100)
    make_asset(conn, category_id, name="Priced B", purchase_cost=50)
    make_asset(conn, category_id, name="Unpriced")

    summary = stats_service.asset_valuation_summary(conn)
    assert summary["total_value"] == 150
    assert summary["priced_count"] == 2
    assert summary["value_checked_out"] == 0


def test_asset_valuation_excludes_soft_deleted(conn, category_id):
    deleted_id = make_asset(conn, category_id, name="Priced then deleted", purchase_cost=500)
    conn.execute("UPDATE assets SET deleted_at = datetime('now') WHERE id = ?", (deleted_id,))
    conn.commit()
    make_asset(conn, category_id, name="Still around", purchase_cost=25)

    summary = stats_service.asset_valuation_summary(conn)
    assert summary["total_value"] == 25
    assert summary["priced_count"] == 1


def test_asset_valuation_rejects_negative_cost(conn, category_id):
    from services.asset_service import create_asset

    result, error = create_asset(
        conn, {"name": "Bad Cost", "category_id": category_id, "purchase_cost": -5}, None, {}, staff_id=None
    )
    assert error is not None
    assert error[1] == 400


def test_asset_valuation_route_returns_priced_assets_only(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    make_asset(conn, category_id, name="Priced", purchase_cost=200)
    make_asset(conn, category_id, name="Unpriced")

    resp = client.get("/api/v1/stats/assets/valuation")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["total_count"] == 1
    assert data["summary"]["total_value"] == 200
    assert len(data["results"]) == 1
    assert data["results"][0]["name"] == "Priced"


def test_asset_valuation_widget_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/stats/export?sections=asset_valuation&format=csv")
    assert resp.status_code == 400
    assert "No exportable sections" in resp.get_json()["error"]
