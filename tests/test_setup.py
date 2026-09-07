"""The fresh-install setup wizard and the live-system admin-recovery path it
shares a route with - routes/setup.py + services/setup_service.py +
services/seed_service.py.

Two distinct "no admin" states this whole module exists to tell apart:
  - fresh install: staff table is completely empty (a brand-new fresh_db, by
    construction - see conftest.py's module docstring: seed_initial_admin()
    only ever runs once, at app import time, against whatever DB existed
    then, never per-test). /setup/complete works with no login at all.
  - recovery: a live system that HAD staff and lost its last active admin.
    Simulated here via seed_service._create_temp_admin() directly (the
    background job that would normally do this, seed_initial_admin(), isn't
    re-run per test - see above). /setup/complete requires being logged in
    as that temp admin.
"""

from tests.conftest import TEST_PASSWORD, login_as, make_staff
from services import seed_service


def complete_setup(client, csrf=None, **overrides):
    data = {
        "first_name": "Real",
        "last_name": "Admin",
        "department_id": overrides.pop("department_id", None),
        "username": "real_admin",
        "password": TEST_PASSWORD,
    }
    data.update(overrides)
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return client.post("/api/v1/setup/complete", json=data, headers=headers)


# # # STATUS / SEED-NOTICE (public, no auth) -----------------------------------


def test_status_reports_fresh_install_on_an_empty_staff_table(client, conn):
    resp = client.get("/api/v1/setup/status")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"setup_required": True, "fresh_install": True}


def test_status_after_an_admin_exists(client, conn, admin_id):
    resp = client.get("/api/v1/setup/status")
    body = resp.get_json()
    assert body == {"setup_required": False, "fresh_install": False}


def test_status_is_setup_required_but_not_fresh_during_recovery(client, conn):
    """Staff table isn't empty (a temp admin exists), but there's still no
    *active, non-seeded* admin - setup_required stays True while
    fresh_install correctly flips to False."""
    seed_service._create_temp_admin(conn)
    conn.commit()
    resp = client.get("/api/v1/setup/status")
    assert resp.get_json() == {"setup_required": True, "fresh_install": False}


def test_seed_notice_empty_when_nothing_pending(client, conn):
    resp = client.get("/api/v1/setup/seed-notice")
    assert resp.status_code == 200
    assert resp.get_json() == {}


def test_seed_notice_never_exposes_a_real_admins_password(client, conn, admin_id):
    """Only ever populated by the temp-admin recovery path - a normal admin
    existing shouldn't produce anything here."""
    resp = client.get("/api/v1/setup/seed-notice")
    assert resp.get_json() == {}


# # # COMPLETE - FRESH INSTALL (no login at all) ------------------------------


def test_complete_setup_fresh_install_creates_first_admin_with_no_login(client, conn, department_id):
    resp = complete_setup(client, department_id=department_id)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert "staff_id" in body

    staff = conn.execute("SELECT * FROM staff WHERE id = ?", (body["staff_id"],)).fetchone()
    assert staff["role"] == "admin"
    assert staff["is_system_seeded"] == 0
    assert staff["username"] == "real_admin"


def test_complete_setup_fresh_install_missing_field_is_rejected(client, conn, department_id):
    resp = complete_setup(client, department_id=department_id, username=None)
    assert resp.status_code == 400
    assert "Missing required field" in resp.get_json()["error"]


def test_complete_setup_fresh_install_weak_password_is_rejected(client, conn, department_id):
    resp = complete_setup(client, department_id=department_id, password="short")
    assert resp.status_code == 400


def test_complete_setup_fresh_install_invalid_department_is_rejected(client, conn):
    resp = complete_setup(client, department_id=999999)
    assert resp.status_code == 400


def test_complete_setup_blocked_once_an_admin_already_exists(client, conn, admin_id, department_id):
    """No longer a fresh install (admin_id fixture created a staff row) --
    is_fresh_install() is now False, so the no-login shortcut no longer
    applies at all and this falls through to the same require_session gate
    the recovery path uses (401, not the "already completed" 403 - that
    one's only reachable by someone who IS logged in, just not as the valid
    temp admin - see test_complete_setup_recovery_rejects_a_non_seeded_
    logged_in_staff below)."""
    resp = complete_setup(client, department_id=department_id)
    assert resp.status_code == 401


# # # COMPLETE - RECOVERY (must be logged in as the temp admin) --------------


def test_complete_setup_recovery_requires_login(client, conn, department_id):
    """Staff table isn't empty (temp admin exists) so is_fresh_install() is
    False - require_session must now gate this, and there's no session."""
    seed_service._create_temp_admin(conn)
    conn.commit()
    resp = complete_setup(client, department_id=department_id)
    assert resp.status_code == 401


def test_complete_setup_recovery_success_retires_the_temp_admin(client, conn, department_id):
    temp_staff_id, _password = seed_service._create_temp_admin(conn)
    temp_member_id = conn.execute(
        "SELECT member_id FROM staff WHERE id = ?", (temp_staff_id,)
    ).fetchone()["member_id"]
    conn.commit()
    csrf = login_as(client, conn, temp_staff_id)

    resp = complete_setup(client, csrf, department_id=department_id)
    assert resp.status_code == 201, resp.get_json()

    assert conn.execute("SELECT id FROM staff WHERE id = ?", (temp_staff_id,)).fetchone() is None
    assert conn.execute("SELECT id FROM members WHERE id = ?", (temp_member_id,)).fetchone() is None

    new_id = resp.get_json()["staff_id"]
    new_staff = conn.execute("SELECT * FROM staff WHERE id = ?", (new_id,)).fetchone()
    assert new_staff["is_system_seeded"] == 0


def test_complete_setup_recovery_clears_the_seed_notice(client, conn, department_id):
    temp_staff_id, password = seed_service._create_temp_admin(conn)
    seed_service._set_seed_notice(conn, seed_service.TEMP_ADMIN_USERNAME, password)
    conn.commit()
    csrf = login_as(client, conn, temp_staff_id)

    assert client.get("/api/v1/setup/seed-notice").get_json() != {}
    complete_setup(client, csrf, department_id=department_id)
    assert client.get("/api/v1/setup/seed-notice").get_json() == {}


def test_complete_setup_recovery_rejects_a_non_seeded_logged_in_staff(
    client, conn, department_id, admin_id
):
    """Logged in, but as a real (non-system-seeded) admin, not the temp
    one - complete_setup must still refuse rather than let any admin
    re-trigger it."""
    csrf = login_as(client, conn, admin_id)
    resp = complete_setup(client, csrf, department_id=department_id)
    assert resp.status_code == 403
    assert "temporary setup admin" in resp.get_json()["reason"]
