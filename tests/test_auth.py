"""Login (username/password + RFID/PIN), lockout, and session/CSRF behavior
-- routes/auth.py + services/login_service.py + services/auth_service.py."""

from datetime import date, timedelta

from werkzeug.security import generate_password_hash

from tests.conftest import TEST_PASSWORD, make_staff, make_member


def login(client, **payload):
    return client.post("/api/v1/login", json=payload)


def login_rfid(client, **payload):
    return client.post("/api/v1/login-rfid", json=payload)


def login_barcode_pin(client, **payload):
    return client.post("/api/v1/login-barcode-pin", json=payload)


def set_rfid_credentials(conn, staff_id, rfid_id, pin):
    """create_staff() doesn't accept/persist rfid_id or rfid_pin (only
    update_staff() and change_own_rfid_pin() do, per staff_service.py) --
    set them directly for tests that need a card+PIN-enabled account."""
    conn.execute(
        "UPDATE staff SET rfid_id = ?, rfid_pin_hash = ? WHERE id = ?",
        (rfid_id, generate_password_hash(pin), staff_id),
    )
    conn.commit()


def set_barcode_pin_credentials(conn, staff_id, pin, enabled=1):
    """Mirrors set_rfid_credentials, but for the all-roles barcode + PIN
    opt-in (0051_add_staff_barcode_pin_enabled.sql) - shares the same
    rfid_pin_hash column, just gated by barcode_pin_enabled instead of role.
    The barcode itself already exists (create_member assigns one), so this
    only needs to set the PIN hash and flip the flag on."""
    conn.execute(
        "UPDATE staff SET rfid_pin_hash = ?, barcode_pin_enabled = ? WHERE id = ?",
        (generate_password_hash(pin), enabled, staff_id),
    )
    conn.commit()


def get_barcode_id(conn, staff_id):
    member_row = conn.execute("SELECT member_id FROM staff WHERE id = ?", (staff_id,)).fetchone()
    return conn.execute(
        "SELECT barcode_id FROM members WHERE id = ?", (member_row["member_id"],)
    ).fetchone()["barcode_id"]


# # # USERNAME/PASSWORD LOGIN --------------------------------------------


def test_login_with_correct_credentials_succeeds(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "logintest")
    resp = login(client, username="logintest", password=TEST_PASSWORD)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["staff_id"] == staff_id
    assert body["role"] == "admin"
    assert "csrf_token" in body
    assert client.get_cookie("session_token") is not None


def test_login_with_wrong_password_fails(client, conn, department_id):
    make_staff(conn, department_id, "admin", "logintest2")
    resp = login(client, username="logintest2", password="wrong-password")
    assert resp.status_code == 401
    assert client.get_cookie("session_token") is None


def test_login_with_unknown_username_fails(client):
    resp = login(client, username="nobody-here", password="whatever")
    assert resp.status_code == 401


def test_login_by_barcode_id_works_too(client, conn, department_id):
    """find_staff_for_login falls back to the linked member's barcode_id."""
    staff_id = make_staff(conn, department_id, "admin", "barcodelogintest")
    member_row = conn.execute("SELECT member_id FROM staff WHERE id = ?", (staff_id,)).fetchone()
    barcode = conn.execute(
        "SELECT barcode_id FROM members WHERE id = ?", (member_row["member_id"],)
    ).fetchone()["barcode_id"]

    resp = login(client, username=barcode, password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_login_missing_password_is_rejected(client):
    resp = client.post("/api/v1/login", json={"username": "someone"})
    assert resp.status_code == 400


def test_deactivated_staff_cannot_log_in(client, conn, department_id):
    make_staff(conn, department_id, "admin", "deactivatedtest", is_active=0)
    resp = login(client, username="deactivatedtest", password=TEST_PASSWORD)
    assert resp.status_code == 403


# # # TOOL KEEPER ASSIGNMENT WINDOW ---------------------------------------


def test_tool_keeper_without_assignment_window_is_rejected(client, conn, department_id):
    make_staff(conn, department_id, "tool_keeper", "tk_no_window")
    resp = login(client, username="tk_no_window", password=TEST_PASSWORD)
    assert resp.status_code == 403
    assert "assignment" in resp.get_json()["error"].lower()


def test_tool_keeper_within_assignment_window_can_log_in(client, conn, department_id):
    today = date.today()
    make_staff(
        conn,
        department_id,
        "tool_keeper",
        "tk_active_window",
        assigned_start=(today - timedelta(days=1)).isoformat(),
        assigned_end=(today + timedelta(days=1)).isoformat(),
    )
    resp = login(client, username="tk_active_window", password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_tool_keeper_with_expired_window_is_rejected(client, conn, department_id):
    today = date.today()
    make_staff(
        conn,
        department_id,
        "tool_keeper",
        "tk_expired_window",
        assigned_start=(today - timedelta(days=10)).isoformat(),
        assigned_end=(today - timedelta(days=1)).isoformat(),
    )
    resp = login(client, username="tk_expired_window", password=TEST_PASSWORD)
    assert resp.status_code == 403
    assert "expired" in resp.get_json()["error"].lower()


def test_tool_keeper_before_window_starts_is_rejected(client, conn, department_id):
    today = date.today()
    make_staff(
        conn,
        department_id,
        "tool_keeper",
        "tk_future_window",
        assigned_start=(today + timedelta(days=1)).isoformat(),
        assigned_end=(today + timedelta(days=10)).isoformat(),
    )
    resp = login(client, username="tk_future_window", password=TEST_PASSWORD)
    assert resp.status_code == 403
    assert "not started" in resp.get_json()["error"].lower()


# # # IN CHARGE ASSIGNMENT WINDOW (optional, but enforced when actually set) --
# Unlike Tool Keeper, a blank window means "no restriction" for In Charge -
# only an account that actually HAS an assigned_end enforces it. This used
# to not be enforced at all (the login route's assignment-window check only
# ever ran for role == "tool_keeper"), so an In Charge account past its own
# expiration date could still log in indefinitely despite showing "expired"
# on their own profile the whole time - see _check_assignment_window in
# routes/auth.py.


def test_in_charge_without_assignment_window_can_log_in(client, conn, department_id):
    """No window set at all - optional for In Charge, so this must NOT be blocked."""
    make_staff(conn, department_id, "in_charge", "ic_no_window")
    resp = login(client, username="ic_no_window", password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_in_charge_within_assignment_window_can_log_in(client, conn, department_id):
    today = date.today()
    make_staff(
        conn,
        department_id,
        "in_charge",
        "ic_active_window",
        assigned_start=(today - timedelta(days=1)).isoformat(),
        assigned_end=(today + timedelta(days=1)).isoformat(),
    )
    resp = login(client, username="ic_active_window", password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_in_charge_with_expired_window_is_rejected(client, conn, department_id):
    """The exact bug this fixes: this used to succeed with 200 before
    _check_assignment_window covered In Charge too."""
    today = date.today()
    make_staff(
        conn,
        department_id,
        "in_charge",
        "ic_expired_window",
        assigned_start=(today - timedelta(days=10)).isoformat(),
        assigned_end=(today - timedelta(days=1)).isoformat(),
    )
    resp = login(client, username="ic_expired_window", password=TEST_PASSWORD)
    assert resp.status_code == 403
    assert "expired" in resp.get_json()["error"].lower()


def test_in_charge_before_window_starts_is_rejected(client, conn, department_id):
    today = date.today()
    make_staff(
        conn,
        department_id,
        "in_charge",
        "ic_future_window",
        assigned_start=(today + timedelta(days=1)).isoformat(),
        assigned_end=(today + timedelta(days=10)).isoformat(),
    )
    resp = login(client, username="ic_future_window", password=TEST_PASSWORD)
    assert resp.status_code == 403
    assert "not started" in resp.get_json()["error"].lower()


def test_in_charge_permanent_sentinel_can_log_in(client, conn, department_id):
    """assigned_end == PERMANENT_ASSIGNED_END (the "Permanent" preset in the
    Add Staff form) means no restriction, same as blank - must not be
    mistaken for an actual expiration date far in the future almost working
    by accident; this confirms it's treated as truly unrestricted."""
    from services.settings_service import PERMANENT_ASSIGNED_END

    today = date.today()
    make_staff(
        conn,
        department_id,
        "in_charge",
        "ic_permanent",
        assigned_start=(today - timedelta(days=1)).isoformat(),
        assigned_end=PERMANENT_ASSIGNED_END,
    )
    resp = login(client, username="ic_permanent", password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_admin_with_expired_window_can_still_log_in(client, conn, department_id):
    """Admin is always exempt (compute_usage_status's "admin is always
    permanent" rule) - even a literal expired assigned_end on file must not
    block them, unlike Tool Keeper/In Charge."""
    today = date.today()
    make_staff(
        conn,
        department_id,
        "admin",
        "admin_expired_window",
        assigned_start=(today - timedelta(days=10)).isoformat(),
        assigned_end=(today - timedelta(days=1)).isoformat(),
    )
    resp = login(client, username="admin_expired_window", password=TEST_PASSWORD)
    assert resp.status_code == 200


def test_barcode_pin_login_also_rejects_expired_assignment_window(client, conn, department_id):
    """The other bypass this closes: login_barcode_pin() had no assignment-
    window check at all, so an expired Tool Keeper/In Charge could sign in
    that way even though the same account correctly gets rejected via
    plain username/password login() above."""
    today = date.today()
    staff_id = make_staff(
        conn,
        department_id,
        "in_charge",
        "ic_barcode_pin_expired",
        assigned_start=(today - timedelta(days=10)).isoformat(),
        assigned_end=(today - timedelta(days=1)).isoformat(),
    )
    set_barcode_pin_credentials(conn, staff_id, "123456")

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="123456")
    assert resp.status_code == 403
    assert "expired" in resp.get_json()["error"].lower()


# # # LOCKOUT ---------------------------------------------------------------


def test_account_locks_after_max_failed_attempts(client, conn, department_id):
    make_staff(conn, department_id, "admin", "lockouttest")

    # DEFAULT_SETTINGS["max_failed_attempts"] is 5 - 4 wrong guesses stay
    # under the threshold, the 5th trips the lock.
    for _ in range(4):
        resp = login(client, username="lockouttest", password="wrong")
        assert resp.status_code == 401

    resp = login(client, username="lockouttest", password="wrong")
    assert resp.status_code == 403
    assert "locked" in resp.get_json()["error"].lower()

    # Locked out now even with the correct password.
    resp = login(client, username="lockouttest", password=TEST_PASSWORD)
    assert resp.status_code == 403


def test_correct_password_resets_failed_attempt_counter(client, conn, department_id):
    make_staff(conn, department_id, "admin", "resettest")

    for _ in range(3):
        login(client, username="resettest", password="wrong")

    resp = login(client, username="resettest", password=TEST_PASSWORD)
    assert resp.status_code == 200

    staff_id = conn.execute("SELECT id FROM staff WHERE username = 'resettest'").fetchone()["id"]
    attempts = conn.execute(
        "SELECT failed_login_attempts FROM staff WHERE id = ?", (staff_id,)
    ).fetchone()["failed_login_attempts"]
    assert attempts == 0


# # # RFID + PIN LOGIN (admin only) ----------------------------------------


def test_rfid_login_succeeds_for_admin_with_correct_pin(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "rfidtest")
    set_rfid_credentials(conn, staff_id, "RFID0001", "123456")

    resp = login_rfid(client, rfid_id="RFID0001", pin="123456")
    assert resp.status_code == 200
    assert resp.get_json()["staff_id"] == staff_id


def test_rfid_login_rejects_wrong_pin(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "rfidwrongpin")
    set_rfid_credentials(conn, staff_id, "RFID0099", "123456")

    resp = login_rfid(client, rfid_id="RFID0099", pin="654321")
    assert resp.status_code == 401


def test_rfid_login_rejects_unknown_card(client):
    resp = login_rfid(client, rfid_id="NO-SUCH-CARD", pin="123456")
    assert resp.status_code == 401


def test_rfid_login_rejects_malformed_pin(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "rfidpintest")
    set_rfid_credentials(conn, staff_id, "RFID0002", "123456")

    resp = login_rfid(client, rfid_id="RFID0002", pin="12")  # too short
    assert resp.status_code == 401


def test_rfid_login_only_matches_admin_role(client, conn, department_id):
    """find_admin_for_rfid_login filters WHERE staff.role = 'admin' - a
    tool_keeper's card, even with the correct PIN, should never
    authenticate through this endpoint."""
    staff_id = make_staff(
        conn, department_id, "tool_keeper", "rfid_tk_test",
        assigned_start=date.today().isoformat(),
        assigned_end=(date.today() + timedelta(days=30)).isoformat(),
    )
    set_rfid_credentials(conn, staff_id, "RFID0003", "123456")

    resp = login_rfid(client, rfid_id="RFID0003", pin="123456")
    assert resp.status_code == 401


# # # BARCODE + PIN LOGIN (opt-in, every role) ------------------------------


def test_barcode_pin_login_succeeds_when_enabled(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "bcpintest")
    set_barcode_pin_credentials(conn, staff_id, "123456")

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="123456")
    assert resp.status_code == 200
    assert resp.get_json()["staff_id"] == staff_id


def test_barcode_pin_login_works_for_non_admin_role(client, conn, department_id):
    """Unlike RFID + PIN, this is open to every role - not filtered to admin."""
    staff_id = make_staff(
        conn, department_id, "tool_keeper", "bcpin_tk_test",
        assigned_start=date.today().isoformat(),
        assigned_end=(date.today() + timedelta(days=30)).isoformat(),
    )
    set_barcode_pin_credentials(conn, staff_id, "123456")

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="123456")
    assert resp.status_code == 200
    assert resp.get_json()["staff_id"] == staff_id


def test_barcode_pin_login_rejects_when_not_enabled(client, conn, department_id):
    """PIN hash is set but barcode_pin_enabled is off (the default) - must not authenticate."""
    staff_id = make_staff(conn, department_id, "admin", "bcpin_disabled_test")
    set_barcode_pin_credentials(conn, staff_id, "123456", enabled=0)

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="123456")
    assert resp.status_code == 401


def test_barcode_pin_login_rejects_wrong_pin(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "bcpinwrongtest")
    set_barcode_pin_credentials(conn, staff_id, "123456")

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="654321")
    assert resp.status_code == 401


def test_barcode_pin_login_rejects_unknown_barcode(client):
    resp = login_barcode_pin(client, barcode_id="NO-SUCH-BARCODE", pin="123456")
    assert resp.status_code == 401


def test_barcode_pin_login_rejects_malformed_pin(client, conn, department_id):
    staff_id = make_staff(conn, department_id, "admin", "bcpinmalformedtest")
    set_barcode_pin_credentials(conn, staff_id, "123456")

    resp = login_barcode_pin(client, barcode_id=get_barcode_id(conn, staff_id), pin="12")  # too short
    assert resp.status_code == 401


# # # SESSION / LOGOUT -------------------------------------------------------


def test_logout_clears_session_cookie(as_admin):
    client, csrf, admin_id = as_admin
    resp = client.post("/api/v1/logout", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    # Session row itself should be gone - a follow-up authenticated request must fail.
    resp2 = client.get("/api/v1/transactions")
    assert resp2.status_code == 401


def test_state_changing_request_without_csrf_token_is_rejected(as_admin):
    client, csrf, admin_id = as_admin
    resp = client.post("/api/v1/logout")  # no X-CSRF-Token header
    assert resp.status_code == 403


def test_state_changing_request_with_wrong_csrf_token_is_rejected(as_admin):
    client, csrf, admin_id = as_admin
    resp = client.post("/api/v1/logout", headers={"X-CSRF-Token": "not-the-real-token"})
    assert resp.status_code == 403


def test_second_login_on_same_browser_is_blocked_while_first_session_is_live(client, conn, department_id):
    """check_existing_session_conflict: single-session-per-device policy."""
    make_staff(conn, department_id, "admin", "conflict_a")
    make_staff(conn, department_id, "admin", "conflict_b")

    first = login(client, username="conflict_a", password=TEST_PASSWORD)
    assert first.status_code == 200

    second = login(client, username="conflict_b", password=TEST_PASSWORD)
    assert second.status_code == 409


# # # PAGE-VS-API AUTH FAILURE (services/auth_service.py's _auth_failure_response) --
# The exact same login_required/require_session check backs both full-page
# routes (routes/pages.py, no /api/ prefix) and every JSON API route
# (everything else, mounted under /api/v1) - so it has to behave
# differently for each: a page load should never render raw JSON as the
# page, a fetch()/htmx call needs the JSON back so static/js/ui.js's
# session-timeout modal can show. Keyed on request path (/api/ or not),
# not headers - see that function's docstring.


def test_unauthenticated_page_request_redirects_to_login(client):
    resp = client.get("/assets", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_unauthenticated_page_request_redirect_includes_next(client):
    resp = client.get("/transactions", follow_redirects=False)
    assert resp.status_code == 302
    assert "next=%2Ftransactions" in resp.headers["Location"] or "next=/transactions" in resp.headers["Location"]


def test_invalid_session_cookie_on_a_page_also_redirects(client):
    client.set_cookie("session_token", "not-a-real-token", domain="localhost")
    resp = client.get("/assets", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].startswith("/login")


def test_unauthenticated_api_request_stays_json_not_a_redirect(client):
    """The exact same auth failure, but under /api/ - must NOT redirect,
    static/js/ui.js's fetch() handlers need the raw JSON body."""
    resp = client.get("/api/v1/transactions", follow_redirects=False)
    assert resp.status_code == 401
    assert resp.get_json() is not None


def test_login_page_carries_next_through_as_a_hidden_field(client):
    resp = client.get("/login?next=/assets")
    assert resp.status_code == 200
    assert b'name="next"' in resp.data
    assert b'value="/assets"' in resp.data


def test_login_page_drops_an_external_next(client):
    """Open-redirect guard: only a same-site relative path is ever echoed
    back into the form."""
    resp = client.get("/login?next=http://evil.example.com")
    assert b'name="next"' not in resp.data


def test_login_page_drops_a_protocol_relative_next(client):
    resp = client.get("/login?next=//evil.example.com")
    assert b'name="next"' not in resp.data


def test_successful_login_redirects_to_next(client, conn, department_id):
    make_staff(conn, department_id, "admin", "next_test_admin")
    resp = client.post(
        "/api/v1/login",
        data={"username": "next_test_admin", "password": TEST_PASSWORD, "next": "/assets"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/assets")


def test_successful_login_ignores_an_external_next(client, conn, department_id):
    """Re-validated server-side too (routes/auth.py's _safe_next_url), not
    just trusted because the login page already filtered it - a
    hand-crafted POST bypassing the page entirely must still be refused."""
    make_staff(conn, department_id, "admin", "next_test_admin2")
    resp = client.post(
        "/api/v1/login",
        data={
            "username": "next_test_admin2",
            "password": TEST_PASSWORD,
            "next": "http://evil.example.com",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "evil.example.com" not in resp.headers["Location"]


def test_successful_login_with_no_next_goes_to_dashboard(client, conn, department_id):
    make_staff(conn, department_id, "admin", "next_test_admin3")
    resp = client.post(
        "/api/v1/login",
        data={"username": "next_test_admin3", "password": TEST_PASSWORD},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "/login" not in resp.headers["Location"]


def test_account_panel_shows_member_barcode_not_staff_code(as_admin, conn, admin_id):
    """The sidebar "My Account" quick panel's BARCODE ID field must show the
    linked member's real barcode_id, not staff.staff_code (a separate
    internal reference code) - regression test for a mislabeled field."""
    client, csrf, _ = as_admin
    row = conn.execute(
        """
        SELECT staff.staff_code, members.barcode_id
        FROM staff JOIN members ON staff.member_id = members.id
        WHERE staff.id = ?
        """,
        (admin_id,),
    ).fetchone()
    assert row["staff_code"] != row["barcode_id"]  # sanity: fixture data actually differs

    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert row["barcode_id"] in html
    assert row["staff_code"] not in html
