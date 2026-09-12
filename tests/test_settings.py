"""Feature flags: defaults, admin-only update (password-gated either
direction), and the before_request blueprint-wide gate they actually
control - services/settings_service.py + routes/staff.py +
each blueprint's `_block_if_<x>_disabled` hook."""

from tests.conftest import TEST_PASSWORD


def update_flags(client, csrf, password=TEST_PASSWORD, **flags):
    return client.post(
        "/api/v1/feature-flags/update",
        json={"current_password": password, "flags": flags},
        headers={"X-CSRF-Token": csrf},
    )


# # # DEFAULTS --------------------------------------------------------------


def test_all_feature_flags_default_to_enabled(as_admin):
    """A missing row should never silently lock a feature out."""
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/transactions")  # any gated route; 200 proves the flag reads as enabled
    assert resp.status_code == 200


# # # UPDATE (admin-only, password-gated) -----------------------------------


def test_update_feature_flags_forbidden_for_non_admin(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = update_flags(client, csrf, assets=False)
    assert resp.status_code == 403


def test_update_feature_flags_requires_correct_password(as_admin):
    client, csrf, _ = as_admin
    resp = update_flags(client, csrf, password="wrong-password", assets=False)
    assert resp.status_code == 401


def test_update_feature_flags_requires_a_password_at_all(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/feature-flags/update",
        json={"flags": {"assets": False}},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_feature_flags_manage_form_includes_reservations_toggle(as_admin):
    """Regression guard: the modal used to have a hardcoded toggle list that
    predated the reservations flag, so it never showed up here even though
    the flag itself worked fine via the API."""
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/feature-flags/manage-form")
    assert resp.status_code == 200
    assert b"flag_reservations" in resp.data


def test_update_feature_flags_success(as_admin, conn):
    client, csrf, _ = as_admin
    resp = update_flags(client, csrf, assets=False)
    assert resp.status_code == 200
    value = conn.execute(
        "SELECT value FROM app_settings WHERE key = 'feature_assets_enabled'"
    ).fetchone()["value"]
    assert value in ("0", "false", "False")


# # # ALARM SETTINGS (admin-only, password-gated) ----------------------------
# Mutes the exit-monitoring alarm itself (independent of the "monitoring"
# feature flag above) - services/staff_service.py's update_alarm_settings.


def update_alarm(client, csrf, password=TEST_PASSWORD, enabled=False):
    return client.post(
        "/api/v1/alarm-settings/update",
        json={"enabled": enabled, "current_password": password},
        headers={"X-CSRF-Token": csrf},
    )


def test_update_alarm_settings_requires_a_password_at_all(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/alarm-settings/update",
        json={"enabled": False},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_update_alarm_settings_requires_correct_password(as_admin):
    client, csrf, _ = as_admin
    resp = update_alarm(client, csrf, password="wrong-password", enabled=False)
    assert resp.status_code == 401


def test_update_alarm_settings_forbidden_for_non_admin(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = update_alarm(client, csrf, enabled=False)
    assert resp.status_code == 403


def test_update_alarm_settings_success(as_admin, conn):
    client, csrf, _ = as_admin
    resp = update_alarm(client, csrf, enabled=False)
    assert resp.status_code == 200
    value = conn.execute("SELECT value FROM app_settings WHERE key = 'alarm_enabled'").fetchone()["value"]
    assert value == "0"


def test_disabling_alarm_settings_clears_an_active_alarm_so_it_cant_resurrect(as_admin, conn):
    """Without this, an active-but-not-yet-cleared alarm at the moment
    someone disables Alarm Settings would just sit there untouched - and
    if the setting gets re-enabled later (possibly much later, once
    whatever caused it is long since resolved), get_alarm_status() would
    report it active again and the Pi's relay-sync loop would re-fire the
    physical buzzer/lamp for an incident nobody intended to re-trigger.
    Disabling the setting now clears it immediately instead, the same way
    a real Master Key Card tap or the software override would."""
    conn.execute(
        "INSERT INTO rfid_events (rfid_tag_scanned, classification, alarm_activated) "
        "VALUES ('TAG-RESURRECT', 'unauthorized', 1)"
    )
    conn.commit()

    client, csrf, _ = as_admin
    resp = update_alarm(client, csrf, enabled=False)
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT alarm_deactivated_at FROM rfid_events WHERE rfid_tag_scanned = 'TAG-RESURRECT'"
    ).fetchone()
    assert row["alarm_deactivated_at"] is not None

    # Re-enabling afterward must not bring it back - the row is already
    # cleared, not just hidden while the setting happens to be off.
    resp = update_alarm(client, csrf, enabled=True)
    assert resp.status_code == 200
    from services.monitoring_service import get_alarm_status

    assert get_alarm_status(conn)["active"] is False


# # # THE GATE ITSELF: does disabling a feature actually block its blueprint? --


def test_disabling_transactions_blocks_the_whole_transactions_blueprint(as_admin):
    client, csrf, _ = as_admin
    assert update_flags(client, csrf, transactions=False).status_code == 200

    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 403
    assert "disabled" in resp.get_json()["error"].lower()


def test_disabling_assets_blocks_the_whole_assets_blueprint(as_admin, asset_id):
    client, csrf, _ = as_admin
    assert update_flags(client, csrf, assets=False).status_code == 200

    resp = client.get(f"/api/v1/assets/{asset_id}")
    assert resp.status_code == 403


def test_disabling_members_blocks_the_whole_members_blueprint(as_admin, member_id):
    client, csrf, _ = as_admin
    assert update_flags(client, csrf, members=False).status_code == 200

    resp = client.get(f"/api/v1/members/{member_id}/profile")
    assert resp.status_code == 403


def test_re_enabling_a_feature_unblocks_it(as_admin):
    client, csrf, _ = as_admin
    update_flags(client, csrf, transactions=False)
    assert client.get("/api/v1/transactions").status_code == 403

    update_flags(client, csrf, transactions=True)
    assert client.get("/api/v1/transactions").status_code == 200


def test_disabling_one_feature_does_not_affect_another(as_admin, asset_id):
    """Each blueprint checks its own flag - disabling transactions
    shouldn't touch assets."""
    client, csrf, _ = as_admin
    update_flags(client, csrf, transactions=False)

    resp = client.get(f"/api/v1/assets/{asset_id}")
    assert resp.status_code == 200


# # # CHECKOUT SESSION TIMEOUT (admin/in_charge-editable) -----------------------


def test_checkout_session_timeout_defaults_to_20(conn):
    from services.settings_service import get_checkout_session_stale_minutes

    assert get_checkout_session_stale_minutes(conn) == 20


def test_update_checkout_session_timeout_success(as_admin, conn):
    from services.settings_service import get_checkout_session_stale_minutes

    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 5},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert get_checkout_session_stale_minutes(conn) == 5


def test_update_checkout_session_timeout_rejects_out_of_range(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 5000},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_update_checkout_session_timeout_rejects_non_integer(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": "soon"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_update_checkout_session_timeout_allowed_for_in_charge(as_in_charge):
    """Operational, not admin-only security policy - same tier as Loan
    Duration/Damage Restriction, unlike Session Settings/Account Security."""
    client, csrf, _ = as_in_charge
    resp = client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 10},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200


def test_update_checkout_session_timeout_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 10},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_checkout_session_actually_expires_using_the_configured_value(as_admin, conn, member_id):
    """End-to-end: set the timeout very low, then confirm a session idle
    past it is lazily closed the next time it's touched - not just that
    the setting round-trips."""
    from tests.test_transactions import start_session

    client, csrf, _ = as_admin
    client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 1},
        headers={"X-CSRF-Token": csrf},
    )

    member_barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()[0]
    session_id = start_session(client, csrf, member_barcode).get_json()["checkout_session_id"]

    # Backdate as if it's been sitting untouched for well past 1 minute.
    conn.execute(
        "UPDATE checkout_sessions SET created_at = datetime('now', 'localtime', '-10 minutes'), "
        "last_activity_at = datetime('now', 'localtime', '-10 minutes') WHERE id = ?",
        (session_id,),
    )
    conn.commit()

    resp = client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": "does-not-matter"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 409
    assert "timed out" in resp.get_json()["error"]

    status = conn.execute("SELECT status FROM checkout_sessions WHERE id = ?", (session_id,)).fetchone()["status"]
    assert status == "cancelled"


def test_manage_form_shows_current_value(as_admin, conn):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/checkout-session-settings/update",
        json={"checkout_session_stale_minutes": 7},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.get("/api/v1/checkout-session-settings/manage-form")
    assert resp.status_code == 200
    assert b"7" in resp.data
