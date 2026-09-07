"""Dashboard's Top Borrowed widget - routes/dashboard.py + services/dashboard_service.py."""


def test_top_borrowed_requires_login(client):
    resp = client.get("/api/v1/dashboard/top-borrowed")
    assert resp.status_code == 401


def test_top_borrowed_default_range(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/dashboard/top-borrowed")
    assert resp.status_code == 200


def test_top_borrowed_each_valid_range(as_admin):
    client, csrf, _ = as_admin
    for range_key in ("today", "yesterday", "7d", "30d"):
        resp = client.get(f"/api/v1/dashboard/top-borrowed?range={range_key}")
        assert resp.status_code == 200, range_key


def test_top_borrowed_rejects_invalid_range(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/dashboard/top-borrowed?range=bogus")
    assert resp.status_code == 400


def test_top_borrowed_reflects_a_real_loan(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
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

    resp = client.get("/api/v1/dashboard/top-borrowed?range=today")
    assert resp.status_code == 200
    asset_name = conn.execute("SELECT name FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
    assert asset_name.encode() in resp.data
