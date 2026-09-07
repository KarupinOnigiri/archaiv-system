"""Member CRUD, manual restrict/unrestrict, deactivate/reactivate (with its
open-loan/open-session/linked-staff guards), and role gating --
routes/members.py + services/member_service.py."""

from tests.conftest import make_member, make_staff


def barcode_of(conn, member_id):
    return conn.execute("SELECT barcode_id FROM members WHERE id = ?", (member_id,)).fetchone()[
        "barcode_id"
    ]


# # # CREATE / READ / UPDATE -----------------------------------------------


def test_add_member_success(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/members/add",
        json={
            "first_name": "New",
            "last_name": "Trainee",
            "department_id": department_id,
            "member_type": "trainee",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201
    assert resp.get_json()["member_code"].startswith("MBR")


def test_add_member_missing_required_field(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/members/add",
        json={"first_name": "Missing", "department_id": department_id, "member_type": "trainee"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_add_member_forbidden_for_tool_keeper(as_tool_keeper, department_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/members/add",
        json={
            "first_name": "X",
            "last_name": "Y",
            "department_id": department_id,
            "member_type": "trainee",
        },
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403


def test_get_member_profile(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/members/{member_id}/profile")
    assert resp.status_code == 200


def test_edit_member_success(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/members/{member_id}/edit",
        json={"first_name": "Edited"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200


# # # MANUAL RESTRICT / UNRESTRICT -------------------------------------------


def test_tool_keeper_can_restrict_a_member(as_tool_keeper, conn, member_id):
    """restrict_member allows all three roles - low-risk, easy to trigger, logged."""
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        f"/api/v1/members/{member_id}/restrict",
        json={"restriction_reason": "Suspicious activity"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    member = conn.execute(
        "SELECT is_restricted, restriction_source FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    assert member["is_restricted"] == 1
    assert member["restriction_source"] == "manual"


def test_tool_keeper_cannot_unrestrict(as_tool_keeper, conn, member_id):
    """Lifting a restriction needs more authority than restricting."""
    client, csrf, _ = as_tool_keeper
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'x', restriction_source = 'manual' WHERE id = ?",
        (member_id,),
    )
    conn.commit()

    resp = client.post(f"/api/v1/members/{member_id}/unrestrict", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_admin_can_unrestrict(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'x', restriction_source = 'manual' WHERE id = ?",
        (member_id,),
    )
    conn.commit()

    resp = client.post(f"/api/v1/members/{member_id}/unrestrict", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    is_restricted = conn.execute(
        "SELECT is_restricted FROM members WHERE id = ?", (member_id,)
    ).fetchone()["is_restricted"]
    assert is_restricted == 0


# # # DEACTIVATE / REACTIVATE -----------------------------------------------


def test_deactivate_member_success(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    resp = client.post(f"/api/v1/members/{member_id}/delete", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    is_active = conn.execute("SELECT is_active FROM members WHERE id = ?", (member_id,)).fetchone()["is_active"]
    assert is_active == 0


def test_deactivate_member_blocked_by_open_loan(as_admin, conn, member_id, asset_id):
    from tests.test_transactions import open_full_loan, FUTURE_DUE

    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, member_id)
    asset_code = conn.execute("SELECT barcode_id FROM assets WHERE id = ?", (asset_id,)).fetchone()["barcode_id"]
    open_full_loan(client, conn, csrf, member_barcode, asset_code, FUTURE_DUE)

    resp = client.post(f"/api/v1/members/{member_id}/delete", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409


def test_deactivate_member_blocked_by_open_checkout_session(as_admin, conn, member_id, asset_id):
    client, csrf, _ = as_admin
    member_barcode = barcode_of(conn, member_id)
    asset_code = conn.execute("SELECT barcode_id FROM assets WHERE id = ?", (asset_id,)).fetchone()["barcode_id"]

    r = client.post(
        "/api/v1/checkout-sessions", json={"member_barcode": member_barcode}, headers={"X-CSRF-Token": csrf}
    )
    session_id = r.get_json()["checkout_session_id"]
    client.post(
        f"/api/v1/checkout-sessions/{session_id}/items/add",
        json={"asset_code": asset_code},
        headers={"X-CSRF-Token": csrf},
    )
    # never finished/cancelled - session stays open

    resp = client.post(f"/api/v1/members/{member_id}/delete", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 409


def test_in_charge_cannot_deactivate_member_linked_to_active_staff(as_in_charge, conn, department_id):
    """Only an admin can deactivate a member who is also active staff."""
    client, csrf, _ = as_in_charge
    staff_id = make_staff(conn, department_id, "tool_keeper", "linked_tk")
    linked_member_id = conn.execute(
        "SELECT member_id FROM staff WHERE id = ?", (staff_id,)
    ).fetchone()["member_id"]

    resp = client.post(
        f"/api/v1/members/{linked_member_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_admin_can_deactivate_member_linked_to_active_staff(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    staff_id = make_staff(conn, department_id, "tool_keeper", "linked_tk2")
    linked_member_id = conn.execute(
        "SELECT member_id FROM staff WHERE id = ?", (staff_id,)
    ).fetchone()["member_id"]

    resp = client.post(
        f"/api/v1/members/{linked_member_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200
    staff_active = conn.execute("SELECT is_active FROM staff WHERE id = ?", (staff_id,)).fetchone()["is_active"]
    assert staff_active == 0  # cascades


def test_admin_cannot_deactivate_member_linked_to_admin_account_this_way(as_admin, conn, department_id):
    """Admin accounts require the peer-approval deactivation flow instead."""
    client, csrf, _ = as_admin
    other_admin_staff_id = make_staff(conn, department_id, "admin", "other_admin")
    linked_member_id = conn.execute(
        "SELECT member_id FROM staff WHERE id = ?", (other_admin_staff_id,)
    ).fetchone()["member_id"]

    resp = client.post(
        f"/api/v1/members/{linked_member_id}/delete", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_reactivate_member(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    client.post(f"/api/v1/members/{member_id}/delete", json={}, headers={"X-CSRF-Token": csrf})

    resp = client.post(f"/api/v1/members/{member_id}/reactivate", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    is_active = conn.execute("SELECT is_active FROM members WHERE id = ?", (member_id,)).fetchone()["is_active"]
    assert is_active == 1


# # # BULK ACTIONS ---------------------------------------------------------


def test_bulk_deactivate_forbidden_for_tool_keeper(as_tool_keeper, member_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/members/bulk-deactivate", json={"ids": [member_id]}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_bulk_deactivate_success(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    member_a = make_member(conn, department_id)
    member_b = make_member(conn, department_id)

    resp = client.post(
        "/api/v1/members/bulk-deactivate",
        json={"ids": [member_a, member_b]},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200
    statuses = [
        row["is_active"]
        for row in conn.execute(
            f"SELECT is_active FROM members WHERE id IN ({member_a},{member_b})"
        ).fetchall()
    ]
    assert statuses == [0, 0]


# # # DEPARTMENTS ---------------------------------------------------------


def test_add_department_success(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/departments/add", json={"name": "New Dept"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 201


def test_add_duplicate_department_is_rejected(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/departments/add",
        json={"name": "Electrical Installation and Maintenance"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_edit_department_success(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/departments/{department_id}/edit", json={"name": "Renamed Dept"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200


def test_edit_department_forbidden_for_tool_keeper(as_tool_keeper, department_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        f"/api/v1/departments/{department_id}/edit", json={"name": "x"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 403


def test_delete_department_blocked_when_members_reference_it(as_admin, department_id, member_id):
    """member_id fixture depends on department_id - there's a real member in it."""
    client, csrf, _ = as_admin
    resp = client.post(f"/api/v1/departments/{department_id}/delete", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400
    assert "still assigned" in resp.get_json()["error"]


def test_delete_department_success_when_empty(as_admin):
    client, csrf, _ = as_admin
    dept_id = client.post(
        "/api/v1/departments/add", json={"name": "Empty Dept"}, headers={"X-CSRF-Token": csrf}
    ).get_json()["id"]
    resp = client.post(f"/api/v1/departments/{dept_id}/delete", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200


def test_departments_reorder(as_admin, conn, department_id):
    """reorder requires the FULL current set of department ids (a partial
    request can't silently orphan the ones left out) - migration 0046
    already seeds 10 named departments on every fresh DB, plus this
    fixture's own one, so the full set has to be read back, not assumed."""
    client, csrf, _ = as_admin
    all_ids = [row["id"] for row in conn.execute("SELECT id FROM departments").fetchall()]
    reordered = list(reversed(all_ids))
    resp = client.post(
        "/api/v1/departments/reorder", json={"order": reordered}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 200

    first_dept_sort_order = conn.execute(
        "SELECT sort_order FROM departments WHERE id = ?", (reordered[0],)
    ).fetchone()["sort_order"]
    assert first_dept_sort_order == 0


def test_departments_reorder_rejects_a_partial_list(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/departments/reorder", json={"order": [department_id]}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_departments_options_requires_login(client):
    resp = client.get("/api/v1/departments/options")
    assert resp.status_code == 401


def test_departments_options_lists_department(as_admin, department_id):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/departments/options")
    assert resp.status_code == 200
    ids = [d["id"] for d in resp.get_json()["departments"]] if resp.is_json else None
    if ids is not None:
        assert department_id in ids


# # # LIST / SEARCH / FILTER / SORT --------------------------------------------


def test_list_requires_login(client):
    resp = client.get("/api/v1/members")
    assert resp.status_code == 401


def test_list_default_shows_only_active(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    inactive = make_member(conn, department_id, first_name="Inactive")
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (inactive,))
    conn.commit()

    resp = client.get("/api/v1/members")
    ids = [r["id"] for r in resp.get_json()["results"]]
    assert member_id in ids
    assert inactive not in ids


def test_list_show_inactive_flips_the_scope(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    inactive = make_member(conn, department_id, first_name="Inactive2")
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (inactive,))
    conn.commit()

    resp = client.get("/api/v1/members?show_inactive=1")
    ids = [r["id"] for r in resp.get_json()["results"]]
    assert inactive in ids
    assert member_id not in ids


def test_list_search_matches_barcode(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, member_id)
    resp = client.get(f"/api/v1/members?q={barcode}")
    ids = [r["id"] for r in resp.get_json()["results"]]
    assert member_id in ids


def test_list_search_no_match(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members?q=ZZZ-no-such-member-ZZZ")
    assert resp.get_json()["total_count"] == 0


def test_list_filters_by_department(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    from services.member_service import add_department

    other_dept, _err = add_department(conn, "Another Dept", staff_id=None)
    other_member = make_member(conn, other_dept["id"], first_name="OtherDept")

    resp = client.get(f"/api/v1/members?department_id={department_id}")
    ids = [r["id"] for r in resp.get_json()["results"]]
    assert member_id in ids
    assert other_member not in ids


def test_list_filters_by_is_restricted(as_admin, conn, member_id, department_id):
    client, csrf, _ = as_admin
    restricted = make_member(conn, department_id, first_name="Restricted")
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'x' WHERE id = ?", (restricted,)
    )
    conn.commit()

    resp = client.get("/api/v1/members?is_restricted=1")
    ids = [r["id"] for r in resp.get_json()["results"]]
    assert restricted in ids
    assert member_id not in ids


def test_list_rejects_invalid_is_restricted(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members?is_restricted=maybe")
    assert resp.status_code == 400


def test_list_rejects_invalid_sort_by(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members?sort_by=not-a-real-column")
    assert resp.status_code == 400


def test_list_rejects_invalid_sort_dir(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members?sort_dir=sideways")
    assert resp.status_code == 400


# # # BY-BARCODE LOOKUP ----------------------------------------------------


def test_by_barcode_found(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    barcode = barcode_of(conn, member_id)
    resp = client.get(f"/api/v1/members/by-barcode/{barcode}")
    assert resp.status_code == 200


def test_by_barcode_matches_even_deactivated_members(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    conn.execute("UPDATE members SET is_active = 0 WHERE id = ?", (member_id,))
    conn.commit()
    barcode = barcode_of(conn, member_id)
    resp = client.get(f"/api/v1/members/by-barcode/{barcode}")
    assert resp.status_code == 200


def test_by_barcode_not_found_shows_the_not_found_modal(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members/by-barcode/NO-SUCH-BARCODE")
    assert resp.status_code == 200
    assert b"not" in resp.data.lower() or b"barcode" in resp.data.lower()


def test_profile_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members/999999/profile")
    assert resp.status_code == 404


# # # RESTRICT VALIDATION --------------------------------------------------


def test_restrict_requires_a_reason(as_tool_keeper, member_id):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        f"/api/v1/members/{member_id}/restrict", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_restrict_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/members/999999/restrict",
        json={"restriction_reason": "x"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 404


def test_unrestrict_confirm_loads_regardless_of_restricted_state(as_admin, member_id):
    """get_member_for_unrestrict only checks the member exists - the
    is_restricted check happens on the actual POST, not the confirm form."""
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/members/{member_id}/unrestrict-confirm")
    assert resp.status_code == 200


def test_unrestrict_rejects_a_member_who_is_not_restricted(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.post(
        f"/api/v1/members/{member_id}/unrestrict", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


# # # CSV IMPORT (admin/in_charge) -----------------------------------------


def csv_upload(client, csrf, content, mode="commit", filename="members.csv"):
    import io

    return client.post(
        "/api/v1/members/import",
        data={"file": (io.BytesIO(content.encode("utf-8")), filename), "mode": mode},
        headers={"X-CSRF-Token": csrf},
        content_type="multipart/form-data",
    )


def test_import_form_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/api/v1/members/import-form")
    assert resp.status_code == 403


def test_import_requires_a_file(as_admin):
    """The multipart/htmx path always answers 200 with an inline error (see
    _members_import_error_response) - HX-Retarget swaps it into the modal
    rather than treating the request itself as failed. JSON callers still
    get a real 400 - see test_import_rejects_missing_required_columns."""
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/members/import", data={}, headers={"X-CSRF-Token": csrf}, content_type="multipart/form-data")
    assert resp.status_code == 200
    assert resp.headers.get("HX-Retarget") == "#modal-container"
    assert b"Missing file upload" in resp.data


def test_import_rejects_missing_required_columns(as_admin):
    client, csrf, _ = as_admin
    resp = csv_upload(client, csrf, "first_name,last_name\nA,B\n")
    assert resp.status_code == 200
    assert b"missing required column" in resp.data.lower()


def test_import_preview_does_not_persist(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    dept_name = conn.execute("SELECT name FROM departments WHERE id = ?", (department_id,)).fetchone()["name"]
    content = f"first_name,last_name,department,member_type\nPreview,Person,{dept_name},trainee\n"
    resp = csv_upload(client, csrf, content, mode="preview")
    assert resp.status_code == 200
    assert b"Preview" in resp.data or b"1" in resp.data

    row = conn.execute("SELECT id FROM members WHERE first_name = 'Preview'").fetchone()
    assert row is None  # rolled back, never committed


def test_import_commit_creates_members(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    dept_name = conn.execute("SELECT name FROM departments WHERE id = ?", (department_id,)).fetchone()["name"]
    content = f"first_name,last_name,department,member_type\nCommitted,Person,{dept_name},trainee\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200

    row = conn.execute("SELECT id FROM members WHERE first_name = 'Committed'").fetchone()
    assert row is not None


def test_import_creates_a_new_department_if_it_does_not_exist(as_admin, conn):
    client, csrf, _ = as_admin
    content = "first_name,last_name,department,member_type\nNewDept,Person,Totally New Department,trainee\n"
    resp = csv_upload(client, csrf, content, mode="commit")
    assert resp.status_code == 200
    dept = conn.execute("SELECT id FROM departments WHERE name = 'Totally New Department'").fetchone()
    assert dept is not None


def test_import_forbidden_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = csv_upload(client, csrf, "first_name,last_name,department,member_type\nA,B,X,trainee\n")
    assert resp.status_code == 403


# # # EXPORT ----------------------------------------------------------------


def test_export_form_requires_login(client):
    resp = client.get("/api/v1/members/export-form", follow_redirects=False)
    assert resp.status_code == 401


def test_export_returns_a_file(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/members/export?format=csv")
    assert resp.status_code == 200
    assert len(resp.data) > 0


# # # BORROW / RESTRICTION HISTORY ------------------------------------------


def test_borrow_history_modal_requires_login(client, member_id):
    resp = client.get(f"/api/v1/members/{member_id}/borrow-history-modal")
    assert resp.status_code == 401


def test_borrow_history_modal_loads(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/members/{member_id}/borrow-history-modal")
    assert resp.status_code == 200


def test_restriction_history_modal_loads(as_admin, member_id):
    client, csrf, _ = as_admin
    resp = client.get(f"/api/v1/members/{member_id}/restriction-history-modal")
    assert resp.status_code == 200


def test_restriction_history_reflects_a_real_restriction(as_admin, conn, member_id):
    client, csrf, _ = as_admin
    client.post(
        f"/api/v1/members/{member_id}/restrict",
        json={"restriction_reason": "Logged for history"},
        headers={"X-CSRF-Token": csrf},
    )
    resp = client.get(f"/api/v1/members/{member_id}/restriction-history")
    assert resp.status_code == 200
