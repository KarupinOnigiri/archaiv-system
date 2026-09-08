"""Staff CRUD, self-service password/RFID-PIN change, lock/unlock,
deactivate/reactivate, and role gating (admin-only for almost everything
here) - routes/staff.py + services/staff_service.py."""

from tests.conftest import TEST_PASSWORD, make_member, make_staff


# # # LIST / READ (admin-only) ---------------------------------------------


def test_staff_list_is_admin_only(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/staff")
    assert resp.status_code == 403


def test_staff_list_success_for_admin(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/staff")
    assert resp.status_code == 200


# # # CREATE --------------------------------------------------------------


def test_add_staff_success(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id, member_type="trainer")
    resp = client.post(
        "/api/v1/staff/add",
        json={
            "member_id": member_id,
            "username": "new_staff_account",
            "password": TEST_PASSWORD,
            "role": "tool_keeper",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201


def test_add_staff_invalid_role_is_rejected(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id, member_type="trainer")
    resp = client.post(
        "/api/v1/staff/add",
        json={
            "member_id": member_id,
            "username": "bad_role",
            "password": TEST_PASSWORD,
            "role": "superuser",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_add_staff_weak_password_is_rejected(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id, member_type="trainer")
    resp = client.post(
        "/api/v1/staff/add",
        json={"member_id": member_id, "username": "weakpw", "password": "abc", "role": "tool_keeper"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_add_staff_unsafe_username_is_rejected(as_admin, conn, department_id):
    """username is later used as a filename component for profile-photo
    uploads (utils/uploads.py's save_staff_photo) - a path separator or
    similar must be rejected before it ever reaches the DB."""
    client, csrf, _ = as_admin
    member_id = make_member(conn, department_id, member_type="trainer")
    resp = client.post(
        "/api/v1/staff/add",
        json={
            "member_id": member_id,
            "username": "../../etc/passwd",
            "password": TEST_PASSWORD,
            "role": "tool_keeper",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_add_staff_duplicate_username_is_rejected(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    make_staff(conn, department_id, "tool_keeper", "dupe_username")
    member_id = make_member(conn, department_id, member_type="trainer")

    resp = client.post(
        "/api/v1/staff/add",
        json={
            "member_id": member_id,
            "username": "dupe_username",
            "password": TEST_PASSWORD,
            "role": "tool_keeper",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_add_staff_forbidden_for_non_admin(as_in_charge, conn, department_id):
    client, csrf, _ = as_in_charge
    member_id = make_member(conn, department_id, member_type="trainer")
    resp = client.post(
        "/api/v1/staff/add",
        json={"member_id": member_id, "username": "x", "password": TEST_PASSWORD, "role": "tool_keeper"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


# # # SELF-SERVICE: PASSWORD / RFID PIN --------------------------------------


def test_change_own_password_success(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/change-password",
        json={"current_password": TEST_PASSWORD, "new_password": "ANewStrongPass1!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200


def test_change_own_password_wrong_current_password(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/change-password",
        json={"current_password": "totally-wrong", "new_password": "ANewStrongPass1!"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401


def test_change_rfid_pin_admin_only(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/staff/change-rfid-pin",
        json={"current_password": TEST_PASSWORD, "new_pin": "654321"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_change_rfid_pin_success_for_admin(as_admin, conn, admin_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/change-rfid-pin",
        json={"current_password": TEST_PASSWORD, "new_pin": "654321"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    pin_hash = conn.execute(
        "SELECT rfid_pin_hash FROM staff WHERE id = ?", (admin_id,)
    ).fetchone()["rfid_pin_hash"]
    assert pin_hash is not None


def test_change_rfid_pin_rejects_malformed_pin(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/change-rfid-pin",
        json={"current_password": TEST_PASSWORD, "new_pin": "12"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


# # # BARCODE + PIN LOGIN (self-service, every role) ------------------------


def test_enable_barcode_pin_login_works_for_non_admin(as_tool_keeper, conn, tool_keeper_id):
    """Unlike change-rfid-pin above, this is open to every role."""
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": TEST_PASSWORD, "new_pin": "111222"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT rfid_pin_hash, barcode_pin_enabled FROM staff WHERE id = ?", (tool_keeper_id,)
    ).fetchone()
    assert row["rfid_pin_hash"] is not None
    assert row["barcode_pin_enabled"] == 1


def test_enable_barcode_pin_login_wrong_current_password(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": "totally-wrong", "new_pin": "111222"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401


def test_enable_barcode_pin_login_rejects_malformed_pin(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": TEST_PASSWORD, "new_pin": "12"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_change_barcode_pin_requires_it_to_be_enabled_first(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/change-barcode-pin",
        json={"current_password": TEST_PASSWORD, "new_pin": "333444"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_change_barcode_pin_success_once_enabled(as_admin, conn, admin_id):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": TEST_PASSWORD, "new_pin": "111222"},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.post(
        "/api/v1/staff/change-barcode-pin",
        json={"current_password": TEST_PASSWORD, "new_pin": "333444"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    assert (
        conn.execute("SELECT barcode_pin_enabled FROM staff WHERE id = ?", (admin_id,)).fetchone()[
            "barcode_pin_enabled"
        ]
        == 1
    )


def test_disable_barcode_pin_login_turns_flag_off_but_keeps_pin_hash(as_admin, conn, admin_id):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": TEST_PASSWORD, "new_pin": "111222"},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.post(
        "/api/v1/staff/barcode-pin/disable",
        json={"current_password": TEST_PASSWORD},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT rfid_pin_hash, barcode_pin_enabled FROM staff WHERE id = ?", (admin_id,)
    ).fetchone()
    assert row["barcode_pin_enabled"] == 0
    # PIN itself stays saved - re-enabling later shouldn't need it set again.
    assert row["rfid_pin_hash"] is not None


def test_disable_barcode_pin_login_wrong_current_password(as_admin):
    client, csrf, _ = as_admin
    client.post(
        "/api/v1/staff/barcode-pin/enable",
        json={"current_password": TEST_PASSWORD, "new_pin": "111222"},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.post(
        "/api/v1/staff/barcode-pin/disable",
        json={"current_password": "totally-wrong"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 401


# # # LOCK / UNLOCK ---------------------------------------------------------


def test_admin_cannot_lock_own_account(as_admin, admin_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/staff/{admin_id}/lock",
        json={"duration_minutes": 10},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_admin_locks_another_staff_account(as_admin, conn, tool_keeper_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/staff/{tool_keeper_id}/lock",
        json={"duration_minutes": 30},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    locked_until = conn.execute(
        "SELECT locked_until FROM staff WHERE id = ?", (tool_keeper_id,)
    ).fetchone()["locked_until"]
    assert locked_until is not None


def test_lock_forbidden_for_non_admin(as_in_charge, tool_keeper_id):
    client, csrf, _ = as_in_charge
    resp = client.post(
        f"/api/v1/staff/{tool_keeper_id}/lock",
        json={"duration_minutes": 10},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_lock_rejects_non_positive_duration(as_admin, tool_keeper_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/staff/{tool_keeper_id}/lock",
        json={"duration_minutes": 0},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_unlock_clears_lockout(as_admin, conn, tool_keeper_id):
    client, csrf, _ = as_admin
    client.post(
        f"/api/v1/staff/{tool_keeper_id}/lock",
        json={"duration_minutes": 30},
        headers={"X-CSRF-Token": csrf},
    )

    resp = client.post(f"/api/v1/staff/{tool_keeper_id}/unlock", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    locked_until = conn.execute(
        "SELECT locked_until FROM staff WHERE id = ?", (tool_keeper_id,)
    ).fetchone()["locked_until"]
    assert locked_until is None


# # # DEACTIVATE / REACTIVATE -----------------------------------------------


def test_deactivate_staff_success(as_admin, conn, tool_keeper_id):
    client, csrf, _ = as_admin
    resp = client.post(f"/api/v1/staff/{tool_keeper_id}/delete", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    is_active = conn.execute(
        "SELECT is_active FROM staff WHERE id = ?", (tool_keeper_id,)
    ).fetchone()["is_active"]
    assert is_active == 0


def test_admin_account_cannot_be_deactivated_via_this_route(as_admin, conn, department_id):
    """Admins only leave via the peer-approval request-deactivation flow."""
    client, csrf, _ = as_admin
    other_admin_id = make_staff(conn, department_id, "admin", "other_admin_del")

    resp = client.post(
        f"/api/v1/staff/{other_admin_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_deactivate_staff_forbidden_for_non_admin(as_in_charge, tool_keeper_id):
    client, csrf, _ = as_in_charge
    resp = client.post(f"/api/v1/staff/{tool_keeper_id}/delete", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_reactivate_requires_password_and_reason(as_admin, conn, tool_keeper_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/staff/{tool_keeper_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/staff/{tool_keeper_id}/reactivate", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_reactivate_success(as_admin, conn, tool_keeper_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/staff/{tool_keeper_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(
        f"/api/v1/staff/{tool_keeper_id}/reactivate",
        json={"current_password": TEST_PASSWORD, "reason": "back from leave"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    is_active = conn.execute(
        "SELECT is_active FROM staff WHERE id = ?", (tool_keeper_id,)
    ).fetchone()["is_active"]
    assert is_active == 1


# # # BULK -------------------------------------------------------------------


def test_bulk_deactivate_skips_admin_accounts(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    tk_id = make_staff(conn, department_id, "tool_keeper", "bulk_tk")
    admin2_id = make_staff(conn, department_id, "admin", "bulk_admin2")

    resp = client.post(
        "/api/v1/staff/bulk-deactivate",
        json={"ids": [tk_id, admin2_id]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    tk_active = conn.execute("SELECT is_active FROM staff WHERE id = ?", (tk_id,)).fetchone()["is_active"]
    admin2_active = conn.execute(
        "SELECT is_active FROM staff WHERE id = ?", (admin2_id,)
    ).fetchone()["is_active"]
    assert tk_active == 0
    assert admin2_active == 1  # admin skipped, not deactivated


# # # CSV IMPORT (admin only - tighter than assets/members' admin+in_charge) --


def csv_upload(client, csrf, content, mode="commit", filename="staff.csv"):
    import io

    return client.post(
        "/api/v1/staff/import",
        data={"file": (io.BytesIO(content.encode("utf-8")), filename), "mode": mode},
        headers={"X-CSRF-Token": csrf},
        content_type="multipart/form-data",
    )


def test_import_form_forbidden_for_in_charge(as_in_charge):
    """Staff import is admin-ONLY, unlike assets/members (admin + in_charge)
    - creating login credentials is more sensitive."""
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/staff/import-form")
    assert resp.status_code == 403


def test_import_requires_a_file(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/staff/import", data={}, headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#modal-container"
    assert b"Missing file upload" in resp.data


def test_import_rejects_missing_required_columns(as_admin):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "username,password\na,b\n")
    assert resp.status_code == 200
    assert b"missing required column" in resp.data.lower()


def test_import_commit_creates_staff_for_an_existing_member(as_admin, conn, member_id):
    """Unlike assets/members import, this links to an EXISTING member by
    barcode/member_code - it never creates a new one."""
    client, csrf, _ = as_admin
    barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()["barcode_id"]
    content = f"member_identifier,username,password,role\n{barcode},imported_tk,{TEST_PASSWORD},tool_keeper\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200

    row = conn.execute("SELECT member_id, role FROM staff WHERE username = 'imported_tk'").fetchone()
    assert row is not None
    assert row["member_id"] == member_id
    assert row["role"] == "tool_keeper"


def test_import_preview_does_not_persist(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()["barcode_id"]
    content = f"member_identifier,username,password,role\n{barcode},preview_tk,{TEST_PASSWORD},tool_keeper\n"
    resp = csv_upload(client, csrf, content, mode="preview")
    assert resp.status_code == 200
    assert conn.execute("SELECT id FROM staff WHERE username = 'preview_tk'").fetchone() is None


def test_import_row_error_for_unknown_member_identifier(as_admin):
    client, csrf, _ = as_admin
    content = f"member_identifier,username,password,role\nNO-SUCH-MEMBER,testuser,{TEST_PASSWORD},tool_keeper\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200
    assert b"No member found" in resp.data


def test_import_row_error_for_invalid_role(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()["barcode_id"]
    content = f"member_identifier,username,password,role\n{barcode},x,{TEST_PASSWORD},not_a_real_role\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200
    assert b"role must be one of" in resp.data


def test_import_row_error_for_weak_password(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()["barcode_id"]
    content = f"member_identifier,username,password,role\n{barcode},x,short,tool_keeper\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200
    assert b"at least 8 characters" in resp.data


def test_import_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = csv_upload(client, csrf, f"member_identifier,username,password,role\nX,x,{TEST_PASSWORD},tool_keeper\n")
    assert resp.status_code == 403


# # # HEADER OVERFLOW MENU (matches Inventory/Members' own consolidation) ----


def test_staff_page_header_has_overflow_menu_and_single_primary_button(as_admin):
    """Import/Export got tucked into one overflow menu (squircle, not
    circle) next to the single primary "Add New Staff" button, same
    consolidation as Inventory's/Members' own headers."""
    client, csrf, _ = as_admin
    resp = client.get("/staff")
    html = resp.get_data(as_text=True)
    assert "btn-square rounded-xl" in html
    assert 'id="import-staff-btn"' in html
    assert 'id="export-staff-btn"' in html
    assert 'id="add-staff-btn"' in html


# # # FULL ACTIVITY MODAL (staff/_full_activity_modal.html) ------------------


def test_full_activity_modal_date_presets_use_shared_soft_tint_highlight(
    as_admin, conn, department_id
):
    """Regression test: this modal's Today/Yesterday presets used a solid
    btn-primary/btn-outline swap for their active state, while every other
    quick-date-filter join in the app (audit_log.html, notifications.html,
    reports.html, monitoring.html, transactions.html) used a soft
    border-primary/bg-primary-10/text-primary tint - a one-off drift from
    that shared convention, per explicit feedback that active-filter
    highlighting should look the same everywhere."""
    other_admin_id = make_staff(conn, department_id, "admin", "activity_modal_target")
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/staff/{other_admin_id}/full-activity-modal")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert "date-quick-btn" in html
    assert "btn-outline" not in html
    start = html.index("function fullActivityUpdateFilterUI")
    body = html[start : start + 1200]
    assert "border-primary" in body
    assert "bg-primary/10" in body
    assert "'btn-primary'" not in body
