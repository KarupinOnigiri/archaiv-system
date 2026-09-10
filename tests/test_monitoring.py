"""RFID exit-monitoring: classification logic, the alarm lifecycle (activate
on detection / deactivate on Master Key Card tap), the two machine-to-machine
listener endpoints (shared-secret auth, not a staff session), the public
alarm-status endpoint's auth-scaled detail, and the Monitoring tab's table
(search/filter/sort) - routes/monitoring.py + services/monitoring_service.py.

Monitoring defaults OFF (migration 0048 seeds feature_monitoring_enabled='0'),
unlike every other feature flag - so unlike other test files, most tests
here call enable_monitoring() first on purpose, and a few deliberately don't
to prove the feature-disabled path actually blocks things.

_VIEW_ROLES in routes/monitoring.py is ('admin', 'in_charge', 'tool_keeper')
-- the app's entire staff role set (see the CHECK constraint in
migrations/0001_initial.sql) - so there is no "role that can't view
Monitoring" to write a negative test against; every staff account can.
"""

from services import monitoring_service
from tests.conftest import TEST_PASSWORD

LISTENER_KEY = "test-listener-key"


def enable_monitoring(conn):
    conn.execute("UPDATE app_settings SET value = '1' WHERE key = 'feature_monitoring_enabled'")
    conn.commit()


def barcode_of(conn, table, entity_id):
    return conn.execute(f"SELECT barcode_id FROM {table} WHERE id = ?", (entity_id,)).fetchone()[
        "barcode_id"
    ]


def borrow_asset(client, csrf, conn, member_id, asset_id):
    """Puts asset_id into 'borrowed' status via the real checkout flow --
    needed so record_rfid_event's authorized branch (which keys off
    assets.status) actually has something to classify as authorized."""
    member_barcode = barcode_of(conn, "members", member_id)
    asset_barcode = barcode_of(conn, "assets", asset_id)

    r = client.post(
        "/api/v1/checkout-sessions",
        json={"member_barcode": member_barcode},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.get_json()
    session_id = r.get_json()["checkout_session_id"]

    r = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_barcode},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.get_json()

    r = client.post(
        f"/api/v1/checkout-sessions/{session_id}/checkout",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 201, r.get_json()


def record_event(client, tag, key=LISTENER_KEY):
    return client.post(
        "/api/v1/rfid-events",
        json={"rfid_tag_scanned": tag},
        headers={"X-RFID-Listener-Key": key},
    )


def deactivate(client, rfid_id, key=LISTENER_KEY):
    return client.post(
        "/api/v1/rfid-events/deactivate",
        json={"rfid_id": rfid_id},
        headers={"X-RFID-Listener-Key": key},
    )


# # # LISTENER AUTH (shared secret, no staff session) -------------------------


def test_record_event_rejects_missing_key(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = client.post("/api/v1/rfid-events", json={"rfid_tag_scanned": "TAG1"})
    assert resp.status_code == 401


def test_record_event_rejects_wrong_key(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = record_event(client, "TAG1", key="wrong-key")
    assert resp.status_code == 401


def test_record_event_fails_closed_when_key_not_configured(client, conn, monkeypatch):
    monkeypatch.delenv("RFID_LISTENER_KEY", raising=False)
    enable_monitoring(conn)
    resp = record_event(client, "TAG1")
    assert resp.status_code == 503


def test_record_event_blocked_when_feature_disabled(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    # Deliberately not calling enable_monitoring() - 0048 seeds it disabled.
    resp = record_event(client, "TAG1")
    assert resp.status_code == 403


def test_record_event_missing_tag_field(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = client.post("/api/v1/rfid-events", json={}, headers={"X-RFID-Listener-Key": LISTENER_KEY})
    assert resp.status_code == 400


def test_deactivate_rejects_missing_key(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = client.post("/api/v1/rfid-events/deactivate", json={"rfid_id": "X"})
    assert resp.status_code == 401


def test_deactivate_missing_rfid_id_field(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = client.post(
        "/api/v1/rfid-events/deactivate", json={}, headers={"X-RFID-Listener-Key": LISTENER_KEY}
    )
    assert resp.status_code == 400


# # # RATE LIMITING -------------------------------------------------------
# Neither M2M endpoint had its own @limiter.limit before - they're already
# gated by a 256-bit shared secret, but nothing throttled repeated hits if
# that key ever leaked. Added per explicit follow-up request; these fire
# real requests up to and past the configured limit rather than just
# checking the decorator is present, so a future edit that loosens/removes
# the limit actually breaks the test.


def test_record_event_is_rate_limited(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    for _ in range(60):
        resp = record_event(client, "TAG1")
        assert resp.status_code != 429
    resp = record_event(client, "TAG1")
    assert resp.status_code == 429


def test_deactivate_is_rate_limited(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    for _ in range(10):
        resp = deactivate(client, "nonexistent-rfid-id")
        assert resp.status_code != 429
    resp = deactivate(client, "nonexistent-rfid-id")
    assert resp.status_code == 429


# # # CLASSIFICATION -----------------------------------------------------------


def test_unregistered_tag_is_unknown(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = record_event(client, "NOPE-NOT-A-TAG")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["classification"] == "unknown_tag"
    assert body["alarm_activated"] is False
    assert body["asset_id"] is None


def test_registered_but_not_borrowed_is_unauthorized(client, conn, asset_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-A' WHERE id = ?", (asset_id,))
    conn.commit()

    resp = record_event(client, "TAG-A")
    body = resp.get_json()
    assert body["classification"] == "unauthorized"
    assert body["alarm_activated"] is True
    assert body["asset_id"] == asset_id


def test_borrowed_asset_is_authorized_with_no_alarm(as_admin, conn, member_id, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-B' WHERE id = ?", (asset_id,))
    conn.commit()
    borrow_asset(client, csrf, conn, member_id, asset_id)

    resp = record_event(client, "TAG-B")
    body = resp.get_json()
    assert body["classification"] == "authorized"
    assert body["alarm_activated"] is False


def test_authorized_event_captures_the_borrower(as_admin, conn, member_id, asset_id, monkeypatch):
    """See migration 0049 - member_id is captured at detection time, not
    looked up live, since the loan can be returned before the log is ever
    viewed."""
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-C' WHERE id = ?", (asset_id,))
    conn.commit()
    borrow_asset(client, csrf, conn, member_id, asset_id)

    record_event(client, "TAG-C")
    row = conn.execute(
        "SELECT member_id FROM rfid_events WHERE rfid_tag_scanned = 'TAG-C'"
    ).fetchone()
    assert row["member_id"] == member_id


def test_unauthorized_event_has_no_borrower_captured(client, conn, asset_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-D' WHERE id = ?", (asset_id,))
    conn.commit()

    record_event(client, "TAG-D")
    row = conn.execute(
        "SELECT member_id FROM rfid_events WHERE rfid_tag_scanned = 'TAG-D'"
    ).fetchone()
    assert row["member_id"] is None


def test_authorized_event_captured_even_if_loan_is_later_returned(
    as_admin, conn, member_id, asset_id, monkeypatch
):
    """The whole reason member_id lives on rfid_events instead of being
    joined live from transactions at view time."""
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-RET' WHERE id = ?", (asset_id,))
    conn.commit()
    borrow_asset(client, csrf, conn, member_id, asset_id)
    record_event(client, "TAG-RET")

    # Return it - the transaction backing that loan flips to 'returned'.
    asset_barcode = barcode_of(conn, "assets", asset_id)
    member_barcode = barcode_of(conn, "members", member_id)
    r = client.post(
        "/api/v1/transactions/return",
        json={"asset_code": asset_barcode, "member_barcode": member_barcode, "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 200, r.get_json()

    row = conn.execute(
        "SELECT member_id FROM rfid_events WHERE rfid_tag_scanned = 'TAG-RET'"
    ).fetchone()
    assert row["member_id"] == member_id


# # # ALARM DEACTIVATE (Master Key Card tap) -----------------------------------


def test_deactivate_clears_active_alarms(client, conn, admin_id, asset_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE staff SET rfid_id = 'ADMIN-CARD' WHERE id = ?", (admin_id,))
    # unauthorized (registered asset, not borrowed) - not unknown_tag, which
    # no longer activates the alarm at all (see monitoring_service.py's
    # record_rfid_event) and so wouldn't have anything here to deactivate.
    conn.execute("UPDATE assets SET rfid_id = 'ASSET-XYZ' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "ASSET-XYZ")

    resp = deactivate(client, "ADMIN-CARD")
    assert resp.status_code == 200
    assert resp.get_json()["deactivated_count"] == 1

    row = conn.execute(
        "SELECT alarm_deactivated_at, deactivated_by FROM rfid_events WHERE rfid_tag_scanned = 'ASSET-XYZ'"
    ).fetchone()
    assert row["alarm_deactivated_at"] is not None
    assert row["deactivated_by"] == admin_id


def test_deactivate_rejects_card_not_registered_to_an_admin(client, conn, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    resp = deactivate(client, "NO-SUCH-CARD")
    assert resp.status_code == 404


def test_deactivate_rejects_a_non_admin_staff_card(client, conn, in_charge_id, monkeypatch):
    """find_admin_for_rfid_login (services/login_service.py) only matches
    role='admin' - an in_charge's own card shouldn't be able to clear an
    alarm, matching the thesis's "Admin's Master Key Card" requirement."""
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE staff SET rfid_id = 'IN-CHARGE-CARD' WHERE id = ?", (in_charge_id,))
    conn.commit()
    resp = deactivate(client, "IN-CHARGE-CARD")
    assert resp.status_code == 404


def test_deactivate_with_no_active_alarm_is_a_no_op(client, conn, admin_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE staff SET rfid_id = 'ADMIN-CARD-2' WHERE id = ?", (admin_id,))
    conn.commit()
    resp = deactivate(client, "ADMIN-CARD-2")
    assert resp.status_code == 200
    assert resp.get_json()["deactivated_count"] == 0


def test_authorized_detection_never_needs_deactivating(
    as_admin, conn, member_id, asset_id, admin_id, monkeypatch
):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-OK' WHERE id = ?", (asset_id,))
    conn.execute("UPDATE staff SET rfid_id = 'ADMIN-CARD-3' WHERE id = ?", (admin_id,))
    conn.commit()
    borrow_asset(client, csrf, conn, member_id, asset_id)

    record_event(client, "TAG-OK")
    resp = deactivate(client, "ADMIN-CARD-3")
    assert resp.get_json()["deactivated_count"] == 0


# # # ALARM SOFTWARE OVERRIDE (password re-confirmation fallback) -------------


def _trigger_unauthorized_alarm(client, conn, asset_id, tag="OVERRIDE-TAG"):
    conn.execute("UPDATE assets SET rfid_id = ? WHERE id = ?", (tag, asset_id))
    conn.commit()
    record_event(client, tag)


def test_override_dismiss_clears_alarm_for_admin(as_admin, conn, asset_id, monkeypatch):
    client, csrf, admin_id = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    _trigger_unauthorized_alarm(client, conn, asset_id)

    resp = client.post(
        "/api/v1/monitoring/alarm/override-dismiss",
        json={"password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["deactivated_count"] == 1
    assert client.get("/api/v1/monitoring/alarm-status").get_json() == {"active": False}

    row = conn.execute(
        "SELECT deactivated_by FROM rfid_events WHERE rfid_tag_scanned = 'OVERRIDE-TAG'"
    ).fetchone()
    assert row["deactivated_by"] == admin_id

    # Logged distinctly from a real Master Key Card tap.
    action = conn.execute(
        "SELECT action FROM audit_log WHERE target_table = 'rfid_events' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert action["action"] == "rfid_alarm_deactivated_override"


def test_override_dismiss_works_for_in_charge_too(as_in_charge, conn, asset_id, monkeypatch):
    client, csrf, _ = as_in_charge
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    _trigger_unauthorized_alarm(client, conn, asset_id, tag="OVERRIDE-TAG-2")

    resp = client.post(
        "/api/v1/monitoring/alarm/override-dismiss",
        json={"password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert resp.get_json()["deactivated_count"] == 1


def test_override_dismiss_rejects_tool_keeper(as_tool_keeper, conn, asset_id, monkeypatch):
    """Deliberately narrower than the roles that can just VIEW Monitoring -
    see routes/monitoring.py's _ALARM_OVERRIDE_ROLES."""
    client, csrf, _ = as_tool_keeper
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    _trigger_unauthorized_alarm(client, conn, asset_id, tag="OVERRIDE-TAG-3")

    resp = client.post(
        "/api/v1/monitoring/alarm/override-dismiss",
        json={"password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403
    # Still active - the rejected request must not have cleared anything.
    assert client.get("/api/v1/monitoring/alarm-status").get_json()["active"] is True


def test_override_dismiss_rejects_wrong_password(as_admin, conn, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    _trigger_unauthorized_alarm(client, conn, asset_id, tag="OVERRIDE-TAG-4")

    resp = client.post(
        "/api/v1/monitoring/alarm/override-dismiss",
        json={"password": "definitely-wrong"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401
    assert client.get("/api/v1/monitoring/alarm-status").get_json()["active"] is True


def test_override_dismiss_requires_password_field(as_admin, conn, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)

    resp = client.post(
        "/api/v1/monitoring/alarm/override-dismiss",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# # # ALARM STATUS (public, detail scaled to who's asking) ---------------------


def test_alarm_status_reachable_without_login(client, conn):
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/alarm-status")
    assert resp.status_code == 200
    assert resp.get_json() == {"active": False}


def test_alarm_status_inactive_when_feature_disabled(client, conn):
    resp = client.get("/api/v1/monitoring/alarm-status")
    assert resp.status_code == 200
    assert resp.get_json() == {"active": False}


def test_alarm_status_unauthenticated_gets_generic_detail_only(client, conn, asset_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-E', name = 'Bench Grinder' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "TAG-E")

    resp = client.get("/api/v1/monitoring/alarm-status")
    body = resp.get_json()
    assert body["active"] is True
    assert body["asset_name"] == "Bench Grinder"
    assert "asset_barcode" in body
    # Deliberately not present for an unauthenticated caller - see
    # routes/monitoring.py's view_alarm_status docstring.
    assert "classification" not in body
    assert "rfid_tag_scanned" not in body
    assert "detected_at" not in body


def test_alarm_status_authenticated_gets_full_detail(as_admin, conn, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-F' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "TAG-F")

    resp = client.get("/api/v1/monitoring/alarm-status")
    body = resp.get_json()
    assert body["active"] is True
    assert body["classification"] == "unauthorized"
    assert body["rfid_tag_scanned"] == "TAG-F"
    assert body["detected_at"]


def test_alarm_status_reflects_deactivation(client, conn, admin_id, asset_id, monkeypatch):
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE staff SET rfid_id = 'ADMIN-CARD-4' WHERE id = ?", (admin_id,))
    # unauthorized, not unknown_tag - see test_deactivate_clears_active_alarms.
    conn.execute("UPDATE assets SET rfid_id = 'ASSET-CLEAR-ME' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "ASSET-CLEAR-ME")

    assert client.get("/api/v1/monitoring/alarm-status").get_json()["active"] is True
    deactivate(client, "ADMIN-CARD-4")
    assert client.get("/api/v1/monitoring/alarm-status").get_json() == {"active": False}


# # # TABLE (search / classification / date / sort) ---------------------------


def test_table_blocked_when_feature_disabled(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/monitoring/table")
    assert resp.status_code == 403


def test_table_shows_recorded_events(as_admin, conn, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-G' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "TAG-G")

    resp = client.get("/api/v1/monitoring/table")
    assert resp.status_code == 200
    assert b"TAG-G" in resp.data


def test_table_filters_by_classification(as_admin, conn, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-H' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "TAG-H")  # unauthorized
    record_event(client, "UNKNOWN-1")  # unknown_tag

    resp = client.get("/api/v1/monitoring/table?classification=unauthorized")
    assert b"TAG-H" in resp.data
    assert b"UNKNOWN-1" not in resp.data


def test_table_rejects_invalid_classification(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?classification=bogus")
    assert resp.status_code == 400


def test_table_rejects_invalid_since_date(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?since=not-a-date")
    assert resp.status_code == 400


def test_table_rejects_invalid_sort_by(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?sort_by=nope")
    assert resp.status_code == 400


def test_table_rejects_invalid_sort_dir(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?sort_dir=sideways")
    assert resp.status_code == 400


def test_table_rejects_invalid_limit(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?limit=notanumber")
    assert resp.status_code == 400


def test_table_search_matches_asset_name(as_admin, conn, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-I', name = 'Impact Wrench' WHERE id = ?", (asset_id,))
    conn.commit()
    record_event(client, "TAG-I")

    resp = client.get("/api/v1/monitoring/table?q=Impact")
    assert b"Impact Wrench" in resp.data


def test_table_search_matches_raw_scanned_tag(as_admin, conn, asset_id, monkeypatch):
    """q also matches rfid_events.rfid_tag_scanned directly, not just the
    matched asset's own identifiers - useful for an unregistered/unknown_tag
    event (nothing else to match against) or when someone's searching a
    printed tag list rather than an asset name."""
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    record_event(client, "UNREGISTERED-TAG-XYZ")

    resp = client.get("/api/v1/monitoring/table?q=UNREGISTERED-TAG-XYZ")
    assert b"UNREGISTERED-TAG-XYZ" in resp.data


def test_table_search_matches_category_name(as_admin, conn, category_id, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-CAT', category_id = ? WHERE id = ?", (category_id, asset_id))
    conn.commit()
    record_event(client, "TAG-CAT")

    resp = client.get("/api/v1/monitoring/table?q=Power+Tools")
    assert b"TAG-CAT" in resp.data


def test_table_search_matches_borrower_name(as_admin, conn, member_id, asset_id, monkeypatch):
    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    conn.execute("UPDATE assets SET rfid_id = 'TAG-J' WHERE id = ?", (asset_id,))
    conn.commit()
    borrow_asset(client, csrf, conn, member_id, asset_id)
    record_event(client, "TAG-J")

    # Matches one field at a time (assets.name / barcode_id / members.first_name
    # / last_name / barcode_id), same as transaction_service's search - not a
    # concatenated full name - so "Member" (conftest's default last_name)
    # alone, not "Test Member" together.
    resp = client.get("/api/v1/monitoring/table?q=Member")
    assert b"TAG-J" in resp.data


def test_table_search_no_match_shows_empty_state(as_admin, conn):
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/api/v1/monitoring/table?q=NoSuchThingAtAll")
    assert b"No detection events match these filters." in resp.data


def test_table_sort_by_asset_name_ascending(as_admin, conn, category_id, monkeypatch):
    from tests.conftest import make_asset

    client, csrf, _ = as_admin
    monkeypatch.setenv("RFID_LISTENER_KEY", LISTENER_KEY)
    enable_monitoring(conn)
    asset_z = make_asset(conn, category_id, name="Zigzag Saw")
    asset_a = make_asset(conn, category_id, name="Angle Grinder")
    conn.execute("UPDATE assets SET rfid_id = 'TAG-Z' WHERE id = ?", (asset_z,))
    conn.execute("UPDATE assets SET rfid_id = 'TAG-AA' WHERE id = ?", (asset_a,))
    conn.commit()
    record_event(client, "TAG-Z")
    record_event(client, "TAG-AA")

    resp = client.get("/api/v1/monitoring/table?sort_by=asset_name&sort_dir=asc")
    body = resp.data.decode()
    assert body.index("Angle Grinder") < body.index("Zigzag Saw")


# # # STAT CARDS (all-time, by classification) -----------------------------


def test_stat_cards_counts_by_classification(conn, asset_id):
    conn.execute("UPDATE assets SET rfid_id = 'TAG-K' WHERE id = ?", (asset_id,))
    conn.commit()
    monitoring_service.record_rfid_event(conn, "TAG-K")  # unauthorized
    monitoring_service.record_rfid_event(conn, "UNKNOWN-3")  # unknown_tag

    stats = monitoring_service.get_monitoring_stat_cards(conn)
    assert stats["stat_total"] == 2
    assert stats["stat_unauthorized"] == 1
    assert stats["stat_unknown"] == 1
    assert stats["stat_authorized"] == 0


def test_stat_cards_include_events_from_previous_days(conn):
    """All-time, not today-scoped (see get_monitoring_stat_cards' own
    comment for why that changed) - an event detected a week ago should
    still count here."""
    conn.execute(
        "INSERT INTO rfid_events (rfid_tag_scanned, classification, alarm_activated, detected_at)"
        " VALUES ('OLD-TAG', 'unknown_tag', 0, datetime('now', '-7 days'))"
    )
    conn.commit()

    stats = monitoring_service.get_monitoring_stat_cards(conn)
    assert stats["stat_total"] == 1
    assert stats["stat_unknown"] == 1


# # # PAGE (routes/pages.py's monitoring_page) ---------------------------------


def test_monitoring_page_blocked_when_feature_disabled(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/monitoring")
    assert resp.status_code == 302  # feature_required's default: redirect to dashboard


def test_monitoring_page_defaults_date_range_to_all_time(as_admin, conn):
    """Used to default to today specifically - changed to all time so the
    page opens on one consistent baseline with get_monitoring_stat_cards'
    own all-time scope (see that function's docstring)."""
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/monitoring")
    assert resp.status_code == 200
    body = resp.data.decode()
    since_input = body.split('id="monitoring-since-filter"')[1].split(">")[0]
    assert 'value=""' in since_input


def test_monitoring_date_quick_filter_uses_shared_soft_tint_highlight(as_admin, conn):
    """Regression test: Monitoring's Today/Yesterday/All quick-date buttons
    used a solid bg-primary/text-primary-content fill for their active
    state, while every other quick-date-filter join in the app (Audit Log,
    Notifications, Reports) used a soft border-primary/bg-primary-10/
    text-primary tint - drifted, inconsistent highlight colors per
    explicit feedback. setDateQuickBtnActive now applies the same
    soft-tint classes as the others."""
    client, csrf, _ = as_admin
    enable_monitoring(conn)
    resp = client.get("/monitoring")
    html = resp.get_data(as_text=True)
    start = html.index("function setDateQuickBtnActive")
    body = html[start : start + 400]
    assert "border-primary" in body
    assert "bg-primary/10" in body
    assert "text-primary" in body and "text-primary-content" not in body
    assert "'bg-primary'" not in body
