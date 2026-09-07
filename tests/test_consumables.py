"""Consumable/quantity-tracked assets (services/asset_service.py's asset_type)
- a second kind of asset row alongside the original serialized ("one row =
one physical, uniquely barcoded/RFID-tagged tool") model. A consumable never
enters the borrowed/reserved/maintenance status lifecycle; checkout
decrements `quantity` directly instead, and it's logged as an already-
'returned' transaction (nothing to return) rather than a new transactions.status
value - see services/transaction_service.py's finish_checkout_session.
"""

from services.asset_service import create_asset, update_asset, adjust_consumable_stock, get_asset_stat_cards
from services.transaction_service import list_available_assets_for_checkout
from tests.conftest import make_asset
from tests.test_transactions import start_session, finish, barcode_of


def add_item(client, csrf, session_id, asset_code, quantity=1, rfid_code=None):
    return client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_code, "rfid_code": rfid_code, "quantity": quantity},
        headers={"X-CSRF-Token": csrf},
    )


def make_consumable(conn, category_id, **overrides):
    overrides.setdefault("quantity", 50)
    overrides.setdefault("reorder_threshold", 10)
    return make_asset(conn, category_id, asset_type="consumable", **overrides)


# # # CREATE / VALIDATION --------------------------------------------------


def test_create_consumable_requires_quantity(conn, category_id):
    result, error = create_asset(
        conn, {"name": "Safety Goggles", "category_id": category_id, "asset_type": "consumable"}, None, {}, staff_id=None
    )
    assert error is not None
    assert error[1] == 400


def test_create_consumable_with_quantity_succeeds(conn, category_id):
    asset_id = make_consumable(conn, category_id, name="Safety Goggles", quantity=50, reorder_threshold=10)
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["asset_type"] == "consumable"
    assert row["quantity"] == 50
    assert row["reorder_threshold"] == 10
    assert row["status"] == "available"


def test_create_serialized_ignores_stray_quantity(conn, category_id):
    """Default asset_type - quantity/reorder_threshold aren't meaningful for
    a uniquely-tagged item, so a stray value is silently dropped, not an error."""
    asset_id = make_asset(conn, category_id, name="Drill", quantity=99)
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["asset_type"] == "serialized"
    assert row["quantity"] is None


def test_create_rejects_invalid_asset_type(conn, category_id):
    result, error = create_asset(
        conn, {"name": "Mystery", "category_id": category_id, "asset_type": "bogus"}, None, {}, staff_id=None
    )
    assert error is not None
    assert error[1] == 400


def test_create_rejects_negative_quantity(conn, category_id):
    result, error = create_asset(
        conn,
        {"name": "Goggles", "category_id": category_id, "asset_type": "consumable", "quantity": -5},
        None, {}, staff_id=None,
    )
    assert error is not None
    assert error[1] == 400


# # # UPDATE / STATUS GUARD -------------------------------------------------


def test_update_asset_rejects_status_change_on_consumable(conn, category_id):
    asset_id = make_consumable(conn, category_id)
    result, error = update_asset(conn, asset_id, {"status": "maintenance"}, None, False, {}, None, staff_id=None)
    assert error is not None
    assert error[1] == 400


def test_update_asset_can_convert_serialized_to_consumable(conn, category_id):
    asset_id = make_asset(conn, category_id, name="Sandpaper Sheets")
    result, error = update_asset(
        conn, asset_id,
        {"asset_type": "consumable", "quantity": 100, "reorder_threshold": 20},
        None, False, {}, None, staff_id=None,
    )
    assert error is None, error
    row = conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert row["asset_type"] == "consumable"
    assert row["quantity"] == 100


# # # STAT CARDS ------------------------------------------------------------


def test_low_stock_count_stat_card(conn, category_id):
    make_consumable(conn, category_id, name="Low Stock Item", quantity=5, reorder_threshold=10)
    make_consumable(conn, category_id, name="Well Stocked Item", quantity=100, reorder_threshold=10)
    make_asset(conn, category_id, name="Regular Tool")

    cards = get_asset_stat_cards(conn)
    assert cards["stat_low_stock"] == 1


# # # ADJUST STOCK ----------------------------------------------------------


def test_adjust_stock_add(conn, category_id):
    asset_id = make_consumable(conn, category_id, quantity=50)
    result, error = adjust_consumable_stock(conn, asset_id, 20, "restocked", staff_id=None)
    assert error is None, error
    assert result["quantity"] == 70


def test_adjust_stock_remove(conn, category_id):
    asset_id = make_consumable(conn, category_id, quantity=50)
    result, error = adjust_consumable_stock(conn, asset_id, -10, "damaged", staff_id=None)
    assert error is None, error
    assert result["quantity"] == 40


def test_adjust_stock_rejects_going_negative(conn, category_id):
    asset_id = make_consumable(conn, category_id, quantity=5)
    result, error = adjust_consumable_stock(conn, asset_id, -10, None, staff_id=None)
    assert error is not None
    assert error[1] == 409


def test_adjust_stock_rejects_serialized_asset(conn, category_id):
    asset_id = make_asset(conn, category_id)
    result, error = adjust_consumable_stock(conn, asset_id, 5, None, staff_id=None)
    assert error is not None
    assert error[1] == 400


# # # CHECKOUT FLOW ---------------------------------------------------------


def test_list_available_assets_includes_consumable_with_stock(conn, category_id):
    make_consumable(conn, category_id, name="Goggles", quantity=10)
    result = list_available_assets_for_checkout(conn)
    names = [a["name"] for a in result["assets"]]
    assert "Goggles" in names


def test_list_available_assets_excludes_out_of_stock_consumable(conn, category_id):
    make_consumable(conn, category_id, name="Empty Goggles", quantity=0)
    result = list_available_assets_for_checkout(conn)
    names = [a["name"] for a in result["assets"]]
    assert "Empty Goggles" not in names


def test_add_consumable_to_cart_does_not_change_status(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code, quantity=5)
    assert resp.status_code == 201, resp.get_json()

    status = conn.execute("SELECT status, quantity FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert status["status"] == "available"
    assert status["quantity"] == 50  # not decremented until commit


def test_rescanning_consumable_increments_cart_quantity(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    assert add_item(client, csrf, session_id, asset_code, quantity=3).status_code == 201
    resp = add_item(client, csrf, session_id, asset_code, quantity=2)
    assert resp.status_code == 201

    item = conn.execute(
        "SELECT quantity FROM checkout_session_items WHERE checkout_session_id = ? AND asset_id = ?",
        (session_id, asset_id),
    ).fetchone()
    assert item["quantity"] == 5


def test_add_consumable_rejects_over_stock_request(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=5)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code, quantity=10)
    assert resp.status_code == 409


def test_checkout_decrements_stock_and_logs_returned_transaction(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    assert add_item(client, csrf, session_id, asset_code, quantity=7).status_code == 201
    resp = finish(client, csrf, session_id)
    assert resp.status_code == 201, resp.get_json()

    quantity = conn.execute("SELECT quantity FROM assets WHERE id = ?", (asset_id,)).fetchone()["quantity"]
    assert quantity == 43

    txn = conn.execute(
        "SELECT status, time_in, due_date FROM transactions WHERE asset_id = ?", (asset_id,)
    ).fetchone()
    assert txn["status"] == "returned"
    assert txn["time_in"] is not None
    assert txn["due_date"] is None


def test_consumable_checkout_does_not_count_toward_loan_limit(as_admin, conn, member_id, category_id):
    """Regression guard for the borrowable_item_count fix - a cart with only
    consumables must not be blocked by max_concurrent_loans."""
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    assert add_item(client, csrf, session_id, asset_code, quantity=1).status_code == 201
    resp = finish(client, csrf, session_id)
    assert resp.status_code == 201, resp.get_json()


def test_remove_item_decrement_by_reduces_quantity_without_deleting_row(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    item_id = add_item(client, csrf, session_id, asset_code, quantity=5).get_json()["item_id"]

    resp = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/{item_id}/remove?decrement_by=2",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["removed"] is False
    assert data["quantity"] == 3

    row = conn.execute("SELECT quantity FROM checkout_session_items WHERE id = ?", (item_id,)).fetchone()
    assert row["quantity"] == 3


def test_remove_item_decrement_by_full_amount_deletes_row(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    item_id = add_item(client, csrf, session_id, asset_code, quantity=3).get_json()["item_id"]

    resp = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/{item_id}/remove?decrement_by=3",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    row = conn.execute("SELECT id FROM checkout_session_items WHERE id = ?", (item_id,)).fetchone()
    assert row is None


def test_consumable_never_shows_up_in_return_flow(as_admin, conn, member_id, category_id):
    client, csrf, _ = as_admin
    asset_id = make_consumable(conn, category_id, quantity=50)
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code, quantity=1)
    finish(client, csrf, session_id)

    open_loan = conn.execute(
        "SELECT id FROM transactions WHERE asset_id = ? AND status = 'borrowed'", (asset_id,)
    ).fetchone()
    assert open_loan is None
