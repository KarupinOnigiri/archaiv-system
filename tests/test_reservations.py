"""Advance reservations - services/reservation_service.py + routes/reservations.py.

Off by default (migration 0057 seeds feature_reservations_enabled='0'), like
monitoring - so, like tests/test_monitoring.py, most tests here call
enable_reservations() first, and a few deliberately don't to prove the
feature-disabled path actually blocks things.

Advisory only (see reservation_service's module docstring): a reservation
never touches assets.status, and doesn't block a normal checkout.
"""

import threading
from datetime import date, timedelta

import app as app_module
from database import get_db_connection
from services import reservation_service
from tests.conftest import make_asset, make_member, login_as
from tests.test_transactions import start_session, add_item, finish, barcode_of

START = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d 09:00:00")
END = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d 12:00:00")


def enable_reservations(conn):
    conn.execute("UPDATE app_settings SET value = '1' WHERE key = 'feature_reservations_enabled'")
    conn.commit()


# # # FEATURE FLAG GATE -----------------------------------------------------


def test_blocked_when_reservations_disabled(as_admin, conn, asset_id, member_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


# # # CREATE -----------------------------------------------------------------


def test_create_reservation_success(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.get_json()

    row = conn.execute("SELECT * FROM reservations WHERE id = ?", (resp.get_json()["id"],)).fetchone()
    assert row["status"] == "scheduled"
    assert row["asset_id"] == asset_id


def test_create_reservation_does_not_touch_asset_status(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    status = conn.execute("SELECT status FROM assets WHERE id = ?", (asset_id,)).fetchone()["status"]
    assert status == "available"


def test_create_reservation_rejects_overlap(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 409


def test_create_reservation_rejects_consumable(as_admin, conn, category_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    consumable_id = make_asset(conn, category_id, asset_type="consumable", quantity=10)
    resp = client.post(
        "/api/v1/reservations",
        json={"asset_id": consumable_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_create_reservation_rejects_end_before_start(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": END, "end_at": START},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# # # ADVISORY-ONLY: CHECKOUT STILL WORKS -------------------------------------


def test_reserved_asset_can_still_be_checked_out(as_admin, conn, asset_id, member_id):
    """The whole point of the v1 scope decision - reservations never block
    the actual checkout CAS."""
    enable_reservations(conn)
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    assert add_item(client, csrf, session_id, asset_code).status_code == 201
    assert finish(client, csrf, session_id).status_code == 201


# # # FULFILLMENT LINKAGE (fulfilled_transaction_id) --------------------------


def test_checkout_fulfills_matching_reservation(as_admin, conn, asset_id, member_id):
    """Checking out an asset that the same member has an active 'scheduled'
    reservation for should auto-mark that reservation 'fulfilled' and link
    it to the new transaction - services/reservation_service.py's
    fulfill_matching_reservation()."""
    enable_reservations(conn)
    client, csrf, _ = as_admin
    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["id"]

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code)
    finish_resp = finish(client, csrf, session_id)
    assert finish_resp.status_code == 201

    reservation = conn.execute("SELECT * FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    assert reservation["status"] == "fulfilled"
    transaction = conn.execute(
        "SELECT id FROM transactions WHERE asset_id = ? AND member_id = ? AND status = 'borrowed'",
        (asset_id, member_id),
    ).fetchone()
    assert reservation["fulfilled_transaction_id"] == transaction["id"]


def test_checkout_does_not_fulfill_other_members_reservation(as_admin, conn, asset_id, member_id):
    """Only the reservation for the SAME member gets linked - a different
    member's booking for the same asset stays 'scheduled'."""
    enable_reservations(conn)
    client, csrf, _ = as_admin
    same_department_id = conn.execute(
        "SELECT department_id FROM members WHERE id = ?", (member_id,)
    ).fetchone()["department_id"]
    other_member_id = make_member(conn, department_id=same_department_id)

    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": other_member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["id"]

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code)
    finish(client, csrf, session_id)

    reservation = conn.execute("SELECT status FROM reservations WHERE id = ?", (reservation_id,)).fetchone()
    assert reservation["status"] == "scheduled"


# # # ADVISORY-ONLY: CHECKOUT-CART HEADS-UP -----------------------------------
# Borrow & Return's own checkout cart has no reservations UI of its own (see
# the project conversation for why that tab stays separate), but scanning a
# reserved asset into it surfaces the same "reserved for" heads-up as the
# Details panel's banner - services/transaction_service.py's
# add_item_to_checkout_session, reservation_notice field.


def test_add_item_includes_reservation_notice(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )

    other_member_id = make_member(conn, conn.execute("SELECT department_id FROM members WHERE id = ?", (member_id,)).fetchone()["department_id"])
    barcode = barcode_of(conn, "members", other_member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 201
    notice = resp.get_json()["reservation_notice"]
    assert notice is not None
    assert notice["start_at"] == START
    assert notice["end_at"] == END
    assert notice["member_name"]


def test_add_item_reservation_notice_absent_when_no_reservation(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 201
    assert resp.get_json()["reservation_notice"] is None


def test_resumed_session_cart_items_include_reservation_notice(as_admin, conn, asset_id, member_id):
    """The same heads-up as a freshly-scanned item, recomputed for an item
    that was already in the cart before the session got minimized/resumed -
    services/transaction_service.py's load_open_sessions_for_staff (backs
    both the dock and /resume-form)."""
    from services.transaction_service import load_open_sessions_for_staff

    enable_reservations(conn)
    client, csrf, staff_id = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )

    other_member_id = make_member(conn, conn.execute("SELECT department_id FROM members WHERE id = ?", (member_id,)).fetchone()["department_id"])
    barcode = barcode_of(conn, "members", other_member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code)

    sessions = load_open_sessions_for_staff(staff_id, conn)
    session = next(s for s in sessions if s["id"] == session_id)
    assert session["cart_items"][0]["reservation_notice"]["start_at"] == START


def test_resumed_session_cart_items_no_notice_when_feature_disabled(as_admin, conn, asset_id, member_id):
    from services.transaction_service import load_open_sessions_for_staff

    enable_reservations(conn)
    client, csrf, staff_id = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )

    other_member_id = make_member(conn, conn.execute("SELECT department_id FROM members WHERE id = ?", (member_id,)).fetchone()["department_id"])
    barcode = barcode_of(conn, "members", other_member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)
    add_item(client, csrf, session_id, asset_code)

    conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'feature_reservations_enabled'")
    conn.commit()

    sessions = load_open_sessions_for_staff(staff_id, conn)
    session = next(s for s in sessions if s["id"] == session_id)
    assert session["cart_items"][0]["reservation_notice"] is None


def test_add_item_reservation_notice_absent_when_feature_disabled(as_admin, conn, asset_id, member_id):
    """A reservation somehow still on record (e.g. the feature was turned
    off after it was made) shouldn't surface a notice while the feature
    itself is off - same as the Details panel banner and Reservations tab
    both being unreachable in that state."""
    enable_reservations(conn)
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    )
    conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'feature_reservations_enabled'")
    conn.commit()

    barcode = barcode_of(conn, "members", member_id)
    session_id = start_session(client, csrf, barcode).get_json()["checkout_session_id"]
    asset_code = barcode_of(conn, "assets", asset_id)

    resp = add_item(client, csrf, session_id, asset_code)
    assert resp.status_code == 201
    assert resp.get_json()["reservation_notice"] is None


# # # CANCEL ------------------------------------------------------------------


def test_cancel_own_reservation(as_tool_keeper, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, staff_id = as_tool_keeper
    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["id"]

    resp = client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200, resp.get_json()

    status = conn.execute("SELECT status FROM reservations WHERE id = ?", (reservation_id,)).fetchone()["status"]
    assert status == "cancelled"


def test_tool_keeper_cannot_cancel_someone_elses_reservation(
    client, second_client, conn, asset_id, member_id, admin_id, tool_keeper_id
):
    # Two independent staff members acting at once - `client`/`second_client`
    # (separate cookie jars), not as_admin + as_tool_keeper (both share the
    # same `client` fixture, so a second login on it just overwrites the
    # first one's session cookie) - see conftest.py's second_client docstring.
    enable_reservations(conn)
    admin_csrf = login_as(client, conn, admin_id)
    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": admin_csrf},
    ).get_json()["id"]

    tk_csrf = login_as(second_client, conn, tool_keeper_id)
    resp = second_client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers={"X-CSRF-Token": tk_csrf})
    assert resp.status_code == 403


def test_admin_can_cancel_anyones_reservation(
    client, second_client, conn, asset_id, member_id, admin_id, tool_keeper_id
):
    enable_reservations(conn)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": tk_csrf},
    ).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers={"X-CSRF-Token": admin_csrf})
    assert resp.status_code == 200


def test_cancel_rejects_already_cancelled(as_admin, conn, asset_id, member_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    reservation_id = client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": csrf},
    ).get_json()["id"]
    client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers={"X-CSRF-Token": csrf})

    resp = client.post(f"/api/v1/reservations/{reservation_id}/cancel", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409


# # # LIST / ROLE SCOPING ------------------------------------------------------


def test_tool_keeper_only_sees_own_reservations(
    client, second_client, conn, asset_id, member_id, admin_id, tool_keeper_id
):
    enable_reservations(conn)
    admin_csrf = login_as(client, conn, admin_id)
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": admin_csrf},
    )

    login_as(second_client, conn, tool_keeper_id)
    resp = second_client.get("/api/v1/reservations")
    assert resp.status_code == 200
    assert resp.get_json()["total_count"] == 0


def test_admin_sees_all_reservations(client, second_client, conn, asset_id, member_id, admin_id, tool_keeper_id):
    enable_reservations(conn)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    client.post(
        "/api/v1/reservations",
        json={"asset_id": asset_id, "member_id": member_id, "start_at": START, "end_at": END},
        headers={"X-CSRF-Token": tk_csrf},
    )

    login_as(second_client, conn, admin_id)
    resp = second_client.get("/api/v1/reservations")
    assert resp.status_code == 200
    assert resp.get_json()["total_count"] == 1


# # # UPCOMING-FOR-ASSET (details modal banner) -------------------------------


def test_upcoming_reservations_for_asset(conn, asset_id, member_id, admin_id):
    enable_reservations(conn)
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (admin_id,)).fetchone()
    result, error = reservation_service.create_reservation(
        conn, asset_id, member_id, START, END, staff
    )
    assert error is None, error

    upcoming = reservation_service.get_upcoming_reservations_for_asset(conn, asset_id)
    assert len(upcoming) == 1
    assert upcoming[0]["member_id"] == member_id


# # # CONCURRENCY (TOCTOU race on the overlap check) --------------------------


def test_concurrent_overlapping_reservations_only_one_succeeds(conn, asset_id, member_id, admin_id):
    """Two staff booking the same overlapping window on the same asset at
    the same instant must not both succeed - regression test for the
    insert-then-reverify race fix in create_reservation()."""
    enable_reservations(conn)
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (admin_id,)).fetchone()

    results = [None, None]

    def attempt(index):
        with app_module.app.app_context():
            thread_conn = get_db_connection()
            try:
                _, error = reservation_service.create_reservation(
                    thread_conn, asset_id, member_id, START, END, staff
                )
                results[index] = error
            finally:
                thread_conn.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = sum(1 for e in results if e is None)
    assert successes == 1, f"expected exactly one reservation to succeed, got results={results}"

    count = conn.execute(
        "SELECT COUNT(*) AS cnt FROM reservations WHERE asset_id = ? AND status = 'scheduled'", (asset_id,)
    ).fetchone()["cnt"]
    assert count == 1


# # # SWEEP EXPIRED -------------------------------------------------------------


def test_sweep_expires_past_due_reservations(conn, asset_id, member_id, admin_id):
    enable_reservations(conn)
    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (admin_id,)).fetchone()
    past_start = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d 09:00:00")
    past_end = (date.today() - timedelta(days=2)).strftime("%Y-%m-%d 12:00:00")
    result, error = reservation_service.create_reservation(
        conn, asset_id, member_id, past_start, past_end, staff
    )
    assert error is None, error
    reservation_id = result["id"]

    reservation_service.sweep_expired_reservations()

    status = conn.execute("SELECT status FROM reservations WHERE id = ?", (reservation_id,)).fetchone()["status"]
    assert status == "expired"


# # # NEW RESERVATION PICKER (asset browse/search) ---------------------------


def test_new_reservation_form_requires_login(client, conn):
    enable_reservations(conn)
    resp = client.get("/api/v1/reservations/new-form")
    assert resp.status_code in (302, 401)


def test_browse_reservable_assets_lists_non_consumables(as_admin, conn, asset_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/reservations/browse-assets")
    assert resp.status_code == 200
    ids = [a["id"] for a in resp.get_json()["assets"]]
    assert asset_id in ids


def test_browse_reservable_assets_excludes_consumables(as_admin, conn, category_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    consumable_id = make_asset(
        conn, category_id, name="Test Gloves", asset_type="consumable", quantity=10, reorder_threshold=2
    )
    resp = client.get("/api/v1/reservations/browse-assets")
    ids = [a["id"] for a in resp.get_json()["assets"]]
    assert consumable_id not in ids


def test_browse_reservable_assets_filters_by_query(as_admin, conn, asset_id, category_id):
    enable_reservations(conn)
    client, csrf, _ = as_admin
    other_id = make_asset(conn, category_id, name="Impact Wrench")
    resp = client.get("/api/v1/reservations/browse-assets?q=Drill")
    ids = [a["id"] for a in resp.get_json()["assets"]]
    assert asset_id in ids
    assert other_id not in ids


def test_reservations_page_renders_new_reservation_button(as_admin, conn):
    """Full-page render sanity check - catches Jinja errors in the button
    markup/shortcut that a JSON-only test wouldn't."""
    enable_reservations(conn)
    client, csrf, _ = as_admin
    resp = client.get("/reservations")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "New Reservation" in html
    assert "new-reservation-btn" in html
