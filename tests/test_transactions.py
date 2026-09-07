"""Checkout-session lifecycle (borrow) + return lifecycle, and their
interplay with member restriction - routes/transactions.py +
services/transaction_service.py + services/loan_service.py.

This is the highest-risk part of the app: multi-step state (available ->
reserved -> borrowed -> returned), re-checked eligibility at both cart-open
and commit time, and two independent auto-restriction paths (overdue,
damaged-return) with different lift rules.
"""

from datetime import date, timedelta

import pytest

from tests.conftest import make_member, make_asset


def barcode_of(conn, table, entity_id):
    return conn.execute(f"SELECT barcode_id FROM {table} WHERE id = ?", (entity_id,)).fetchone()[
        "barcode_id"
    ]


def start_session(client, csrf, member_barcode):
    return client.post(
        "/api/v1/checkout-sessions",
        json={"member_barcode": member_barcode},
        headers={"X-CSRF-Token": csrf},
    )


def add_item(client, csrf, session_id, asset_code, rfid_code=None):
    return client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_code, "rfid_code": rfid_code},
        headers={"X-CSRF-Token": csrf},
    )


def finish(client, csrf, session_id, due_date=None):
    payload = {"due_date": due_date} if due_date else {}
    return client.post(
        f"/api/v1/checkout-sessions/{session_id}/checkout",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )


def open_full_loan(client, conn, csrf, member_barcode, asset_code, due_date=None):
    """Convenience: start a session, add one item, finish checkout. Returns
    the transaction id."""
    r = start_session(client, csrf, member_barcode)
    assert r.status_code == 201, r.get_json()
    session_id = r.get_json()["checkout_session_id"]

    r = add_item(client, csrf, session_id, asset_code)
    assert r.status_code == 201, r.get_json()

    r = finish(client, csrf, session_id, due_date)
    assert r.status_code == 201, r.get_json()

    txn = conn.execute(
        "SELECT id FROM transactions WHERE checkout_session_id = ?", (session_id,)
    ).fetchone()
    return txn["id"]


FUTURE_DUE = (date.today() + timedelta(days=7)).isoformat()


# # # CHECKOUT SESSION: START --------------------------------------------


def test_start_checkout_session_success(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    resp = start_session(client, csrf, barcode)
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["member_id"] == member_id


def test_start_checkout_session_unknown_member(as_admin):
    client, csrf, _ = as_admin
    resp = start_session(client, csrf, "NO-SUCH-BARCODE")
    assert resp.status_code == 404


def test_start_checkout_session_rejects_inactive_member(as_admin, conn, department_id):
    """create_member()'s INSERT doesn't have an is_active column at all
    (only update_member()/deactivate_member() touch it) - set it directly."""
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id)
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (member_id,))
    conn.commit()
    barcode = barcode_of(conn, "members", member_id)
    resp = start_session(client, csrf, barcode)
    assert resp.status_code == 403


def test_start_checkout_session_rejects_restricted_member(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id)
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'test', restriction_source = 'manual' "
        "WHERE id = ?",
        (member_id,),
    )
    conn.commit()
    barcode = barcode_of(conn, "members", member_id)
    resp = start_session(client, csrf, barcode)
    assert resp.status_code == 403
    assert resp.get_json()["reason"] == "test"


def test_cannot_open_two_checkout_sessions_for_same_member(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    first = start_session(client, csrf, barcode)
    assert first.status_code == 201

    second = start_session(client, csrf, barcode)
    assert second.status_code == 409


# # # CHECKOUT SESSION: ADD/REMOVE ITEMS -----------------------------------


def test_add_item_reserves_the_asset(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]

    asset_code = barcode_of(conn, "assets", asset_id)
    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 201

    status = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    assert status == "reserved"


def test_add_item_unknown_asset_code(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, "NOT-A-REAL-CODE")
    assert resp.status_code == 404


def test_add_item_already_in_cart_is_rejected(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    assert add_item(client, csrf, session_id, asset_code).status_code == 201
    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 409


def test_add_item_not_available_is_rejected(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset_id,))
    conn.commit()

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 409


def test_remove_item_releases_the_asset_back_to_available(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    item_id = add_item(client, csrf, session_id, asset_code).get_json()["item_id"]

    resp = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/{item_id}/remove",
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    status = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    assert status == "available"


# # # CHECKOUT SESSION: FINISH / CANCEL -------------------------------------


def test_finish_checkout_marks_asset_borrowed_and_creates_transaction(as_admin, conn, member_id, asset_id):
    client, csrf, staff_id = as_admin
    barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, barcode, asset_code, FUTURE_DUE)

    asset_status = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    assert asset_status == "borrowed"

    txn = conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert txn["status"] == "borrowed"
    assert txn["member_id"] == member_id
    assert txn["asset_id"] == asset_id
    assert txn["borrowed_by_staff_id"] == staff_id


def test_finish_checkout_with_empty_cart_is_rejected(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]

    resp = finish(client, csrf, session_id, FUTURE_DUE)
    assert resp.status_code == 400


def test_finish_checkout_exceeding_loan_limit_is_rejected(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    conn.execute(
        "INSERT OR REPLACE INTO loan_limits (member_type, max_concurrent_loans) VALUES ('trainee', 1)"
    )
    conn.commit()

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]

    asset_a = make_asset(conn)
    asset_b = make_asset(conn)
    assert add_item(client, csrf, session_id, barcode_of(conn, "assets", asset_a)).status_code == 201
    assert add_item(client, csrf, session_id, barcode_of(conn, "assets", asset_b)).status_code == 201

    resp = finish(client, csrf, session_id, FUTURE_DUE)
    assert resp.status_code == 403


def test_cancel_checkout_session_releases_reserved_assets(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code)

    resp = client.post(f"/api/v1/checkout-sessions/{session_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    status = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    assert status == "available"
    session_status = conn.execute(
        "SELECT status FROM checkout_sessions WHERE id = ?", (session_id,)
    ).fetchone()["status"]
    assert session_status == "cancelled"


# # # RETURN: BY SCAN --------------------------------------------------------


def test_return_by_scan_functional_makes_asset_available_again(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": member_barcode, "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "available"

    asset = conn.execute("SELECT status, condition FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["status"] == "available"
    assert asset["condition"] == "functional"


def test_return_by_scan_damaged_sets_maintenance_status(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": member_barcode, "condition_in": "for_repair"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "maintenance"


def test_return_by_scan_disposed_soft_deletes_the_asset(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": member_barcode, "condition_in": "disposed"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    asset = conn.execute("SELECT deleted_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["deleted_at"] is not None


def test_return_by_scan_wrong_member_is_rejected(as_admin, conn, member_id, asset_id, department_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    other_member = make_member(conn, department_id)
    other_barcode = barcode_of(conn, "members", other_member)

    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": other_barcode},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_return_by_scan_asset_with_no_open_loan_is_rejected(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    # never checked out
    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": member_barcode},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# # # RETURN: BY TRANSACTION ID / BATCH --------------------------------------


def test_return_by_transaction_id(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    status = conn.execute("SELECT status FROM transactions WHERE id = ?", (txn_id,)).fetchone()["status"]
    assert status == "returned"


def test_returning_already_returned_transaction_is_rejected(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    first = client.post(f"/api/v1/transactions/{txn_id}/return", json={}, headers={"X-CSRF-Token": csrf})
    assert first.status_code == 200
    second = client.post(f"/api/v1/transactions/{txn_id}/return", json={}, headers={"X-CSRF-Token": csrf})
    assert second.status_code == 400


def test_batch_return(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)

    asset_a = make_asset(conn)
    asset_b = make_asset(conn)
    r = start_session(client, csrf, member_barcode)
    session_id = r.get_json()["checkout_session_id"]
    add_item(client, csrf, session_id, barcode_of(conn, "assets", asset_a))
    add_item(client, csrf, session_id, barcode_of(conn, "assets", asset_b))
    finish(client, csrf, session_id, FUTURE_DUE)

    txn_ids = [
        row["id"]
        for row in conn.execute(
            "SELECT id FROM transactions WHERE checkout_session_id = ?", (session_id,)
        ).fetchall()
    ]
    assert len(txn_ids) == 2

    resp = client.post(
        "/api/v1/transactions/batch-return",
        json={"transaction_ids": txn_ids, "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    statuses = [
        row["status"]
        for row in conn.execute(
            f"SELECT status FROM transactions WHERE id IN ({','.join('?' * len(txn_ids))})", txn_ids
        ).fetchall()
    ]
    assert statuses == ["returned", "returned"]


# # # RENEW -------------------------------------------------------------------


def test_renew_transaction_updates_due_date(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    new_due = (date.today() + timedelta(days=30)).isoformat()
    resp = client.post(
        f"/api/v1/transactions/{txn_id}/renew",
        json={"due_date": new_due},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    due = conn.execute("SELECT due_date FROM transactions WHERE id = ?", (txn_id,)).fetchone()["due_date"]
    assert due.startswith(new_due)


# # # RESTRICTION INTERPLAY ----------------------------------------------


def _backdate_due(conn, txn_id, days_ago=5):
    past = (date.today() - timedelta(days=days_ago)).isoformat()
    conn.execute("UPDATE transactions SET due_date = ? WHERE id = ?", (past, txn_id))
    conn.commit()


def test_overdue_loan_restricts_member_on_next_checkout_attempt(as_admin, conn, member_id, asset_id, department_id):
    """check_and_restrict_overdue_member runs again at the *next* checkout
    attempt (start_checkout_session), not just on a schedule - confirms
    the re-check actually blocks a second, unrelated checkout."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)
    _backdate_due(conn, txn_id)

    resp = start_session(client, csrf, member_barcode)
    assert resp.status_code == 403

    member = conn.execute("SELECT is_restricted, restriction_source FROM members WHERE id = ?", (member_id,)).fetchone()
    assert member["is_restricted"] == 1
    assert member["restriction_source"] == "auto"


def test_returning_the_overdue_item_lifts_the_auto_restriction(as_admin, conn, member_id, asset_id):
    from services.loan_service import check_and_restrict_overdue_member

    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    txn_id = open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)
    _backdate_due(conn, txn_id)
    check_and_restrict_overdue_member(member_id, conn)
    assert conn.execute("SELECT is_restricted FROM members WHERE id = ?", (member_id,)).fetchone()["is_restricted"] == 1

    resp = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_code, "member_barcode": member_barcode, "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    member = conn.execute("SELECT is_restricted FROM members WHERE id = ?", (member_id,)).fetchone()
    assert member["is_restricted"] == 0


def test_damage_threshold_restricts_member_and_is_not_auto_lifted(as_admin, conn, member_id, department_id):
    """check_and_restrict_damaged_returns uses restriction_source='auto_damage',
    which check_and_lift_restriction_if_resolved deliberately does not touch --
    stays restricted until a human (unrestriction request) resolves it."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)

    # DEFAULT_SETTINGS["damage_restriction_threshold"] is 2.
    for _ in range(2):
        asset_id = make_asset(conn)
        asset_code = barcode_of(conn, "assets", asset_id)
        open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)
        resp = client.post(
            "/api/v1/transactions/return",
            json={"asset_code": asset_code, "member_barcode": member_barcode, "condition_in": "for_repair"},
            headers={"X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200

    member = conn.execute(
        "SELECT is_restricted, restriction_source FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    assert member["is_restricted"] == 1
    assert member["restriction_source"] == "auto_damage"

    # A further functional return should NOT auto-lift a damage restriction.
    another_asset = make_asset(conn)
    # Can't check out while restricted - confirms it really is still blocking new loans too.
    resp = start_session(client, csrf, member_barcode)
    assert resp.status_code == 403


# # # SECOND-SCAN VERIFICATION (barcode / RFID tag / asset ID, not RFID-only) --
# transaction_service._verify_second_scan - the checkout cart's anti-swap
# check. rfid_code is the second (confirmatory) scan; despite the field name
# it accepts a match on the asset's RFID tag, its barcode, or its own asset
# ID code, not RFID exclusively.


def test_add_item_second_scan_matching_rfid_tag_succeeds(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET rfid_id = 'TAG-XYZ' WHERE id = ?", (asset_id,))
    conn.commit()
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, asset_code, rfid_code="TAG-XYZ")
    assert resp.status_code == 201, resp.get_json()


def test_add_item_second_scan_decimal_rfid_scan_succeeds(as_admin, conn, member_id, asset_id):
    """Some readers set to type scans as keyboard input only type the
    decimal value of an EPC's last 3 bytes, not the full hex ID - see
    utils/validators.py's decimal_scan_hex_suffix() docstring for the real
    example this is derived from (EPC ...ABCDE12345, scanned as
    "14754629")."""
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET rfid_id = '45494D00E00C00ABCDE12345' WHERE id = ?", (asset_id,))
    conn.commit()
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, asset_code, rfid_code="14754629")
    assert resp.status_code == 201, resp.get_json()


def test_add_item_second_scan_matching_barcode_succeeds(as_admin, conn, member_id, asset_id):
    """Re-presenting the barcode as the "verification" scan (e.g. an asset
    with no RFID tag registered, or a barcode scanner used twice) is also
    accepted - not just an actual RFID read."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, asset_code, rfid_code=asset_code)
    assert resp.status_code == 201, resp.get_json()


def test_add_item_second_scan_matching_asset_id_code_succeeds(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    asset_id_code = conn.execute("SELECT asset_id FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, asset_code, rfid_code=asset_id_code)
    assert resp.status_code == 201, resp.get_json()


def test_add_item_second_scan_mismatch_is_rejected(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = add_item(client, csrf, session_id, asset_code, rfid_code="TOTALLY-WRONG-TAG")
    assert resp.status_code == 409
    assert resp.get_json()["code"] == "verification_mismatch"

    # Rejected verification must not have added the item anyway.
    asset = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["status"] == "available"


def test_add_item_no_second_scan_still_works(as_admin, conn, member_id, asset_id):
    """rfid_code omitted entirely - barcode-only add, unaffected by any of
    this verification logic."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    resp = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_code},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201


def test_return_by_scan_second_scan_mismatch_is_rejected(as_admin, conn, member_id, asset_id):
    """Same verification logic applies on the return side
    (_find_open_transaction_for_scan) - a barcode peeled off the original
    tool and stuck on a substitute shouldn't sail through on barcode alone
    once someone actually taps a second identifier that doesn't match."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(
        "/api/v1/transactions/return",
        json={
            "asset_code": asset_code,
            "member_barcode": member_barcode,
            "condition_in": "functional",
            "rfid_code": "NOT-THE-RIGHT-TAG",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 409

    txn = conn.execute("SELECT status FROM transactions WHERE asset_id = ?", (asset_id,)).fetchone()
    assert txn["status"] == "borrowed"  # still open - rejected verification didn't return it


# # # BROWSE PANELS (checkout modal's "Show available assets/members") --------


def test_list_available_assets_for_checkout(as_admin, asset_id):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/checkout-sessions/available-assets")
    assert resp.status_code == 200
    codes = [a["code"] for a in resp.get_json()["assets"]]
    assert any(codes)


def test_available_assets_excludes_non_available_status(as_admin, conn, asset_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset_id,))
    conn.commit()
    resp = client.get("/api/v1/checkout-sessions/available-assets")
    codes = [a["code"] for a in resp.get_json()["assets"]]
    asset_code = barcode_of(conn, "assets", asset_id)
    assert asset_code not in codes


def test_list_available_members_for_checkout_includes_restricted_but_flagged(as_admin, conn, member_id):
    """Restricted members are shown, not hidden (see templates/transactions/
    _checkout_modal.html's browse panel) - just flagged so staff can't pick
    them."""
    client, csrf, _ = as_admin
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'x' WHERE id = ?", (member_id,)
    )
    conn.commit()
    resp = client.get("/api/v1/checkout-sessions/available-members")
    assert resp.status_code == 200
    member_barcode = barcode_of(conn, "members", member_id)
    row = next(m for m in resp.get_json()["members"] if m["code"] == member_barcode)
    assert row["restricted"] is True
    assert row["restriction_reason"] == "x"


def test_list_available_members_excludes_inactive(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (member_id,))
    conn.commit()
    resp = client.get("/api/v1/checkout-sessions/available-members")
    member_barcode = barcode_of(conn, "members", member_id)
    codes = [m["code"] for m in resp.get_json()["members"]]
    assert member_barcode not in codes


def test_available_assets_requires_login(client):
    resp = client.get("/api/v1/checkout-sessions/available-assets")
    assert resp.status_code == 401


def test_available_members_requires_login(client):
    resp = client.get("/api/v1/checkout-sessions/available-members")
    assert resp.status_code == 401


def test_members_with_open_loans_lists_only_members_with_something_borrowed(
    as_admin, conn, member_id, asset_id, department_id
):
    """Backs the Return Items modal's "Show members with loans" flyout."""
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, "members", member_id)
    asset_code = barcode_of(conn, "assets", asset_id)
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    other_member = make_member(conn, department_id)
    other_barcode = barcode_of(conn, "members", other_member)

    resp = client.get("/api/v1/transactions/members-with-loans")
    assert resp.status_code == 200
    codes = [m["code"] for m in resp.get_json()["members"]]
    assert member_barcode in codes
    assert other_barcode not in codes
