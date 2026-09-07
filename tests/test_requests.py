"""Member unrestriction requests - the tool_keeper-files, admin/in_charge-
reviews approval flow (the "Approvals" tab) - routes/requests.py +
services/unrestriction_service.py.

Several tests here need two DIFFERENT staff members acting at once (one
files, another reviews) - `client` + `second_client` (tests/conftest.py)
give each actor its own cookie jar. Using two `as_<role>` fixtures together
on the same test looks tempting but is wrong: they share the single
`client` fixture, so the second login just overwrites the first one's
session cookie in that one jar rather than adding a second concurrent
session - whichever fixture resolves last silently becomes "both" actors.
"""

from tests.conftest import TEST_PASSWORD, login_as, make_member, make_staff


def restrict(conn, member_id, reason="Overdue item"):
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = ?, restriction_source = 'manual' WHERE id = ?",
        (reason, member_id),
    )
    conn.commit()


def create_request(client, csrf, member_id, reason="Paid the fine"):
    return client.post(
        "/api/v1/unrestriction-requests",
        json={"member_id": member_id, "reason": reason},
        headers={"X-CSRF-Token": csrf},
    )


# # # FEATURE FLAG GATE --------------------------------------------------------


def test_blocked_when_requests_feature_disabled(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    conn.execute("UPDATE app_settings SET value = '0' WHERE key = 'feature_requests_enabled'")
    conn.commit()
    resp = create_request(client, csrf, member_id)
    assert resp.status_code == 403


# # # CREATE (tool_keeper only) ------------------------------------------------


def test_create_success(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    resp = create_request(client, csrf, member_id)
    assert resp.status_code == 201, resp.get_json()

    row = conn.execute(
        "SELECT status FROM unrestriction_requests WHERE id = ?", (resp.get_json()["id"],)
    ).fetchone()
    assert row["status"] == "pending"


def test_create_forbidden_for_admin(as_admin, conn, member_id):
    """Filing is tool_keeper-only - an admin unrestricts directly instead."""
    client, csrf, _ = as_admin
    restrict(conn, member_id)
    resp = create_request(client, csrf, member_id)
    assert resp.status_code == 403


def test_create_missing_member_id(as_tool_keeper, conn):
    client, csrf, _ = as_tool_keeper
    resp = client.post(
        "/api/v1/unrestriction-requests", json={"reason": "x"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_create_missing_reason(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    resp = client.post(
        "/api/v1/unrestriction-requests", json={"member_id": member_id}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_create_member_not_found(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = create_request(client, csrf, 999999)
    assert resp.status_code == 404


def test_create_rejects_a_member_who_is_not_restricted(as_tool_keeper, member_id):
    client, csrf, _ = as_tool_keeper
    resp = create_request(client, csrf, member_id)
    assert resp.status_code == 400


def test_create_rejects_a_duplicate_pending_request(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    r1 = create_request(client, csrf, member_id)
    assert r1.status_code == 201
    r2 = create_request(client, csrf, member_id)
    assert r2.status_code == 409


# # # CANCEL (requester only) --------------------------------------------------


def test_cancel_by_requester_succeeds(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    req_id = create_request(client, csrf, member_id).get_json()["id"]

    resp = client.post(f"/api/v1/unrestriction-requests/{req_id}/cancel", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200
    row = conn.execute("SELECT status FROM unrestriction_requests WHERE id = ?", (req_id,)).fetchone()
    assert row["status"] == "cancelled"


def test_cancel_by_someone_else_is_forbidden(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/cancel", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 403


def test_cancel_not_found(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post("/api/v1/unrestriction-requests/999999/cancel", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 404


def test_cancel_already_reviewed_request_is_rejected(
    client, second_client, conn, department_id, tool_keeper_id, admin_id
):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    approve = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert approve.status_code == 200

    resp = client.post(f"/api/v1/unrestriction-requests/{req_id}/cancel", json={}, headers={"X-CSRF-Token": tk_csrf})
    assert resp.status_code == 409


# # # LIST (scoped by role) ----------------------------------------------------


def test_list_tool_keeper_only_sees_their_own(client, second_client, conn, department_id):
    tk1 = make_staff(conn, department_id, "tool_keeper", "tk1")
    tk2 = make_staff(conn, department_id, "tool_keeper", "tk2")
    m1 = make_member(conn, department_id, first_name="One")
    m2 = make_member(conn, department_id, first_name="Two")
    restrict(conn, m1)
    restrict(conn, m2)

    csrf1 = login_as(client, conn, tk1)
    create_request(client, csrf1, m1)

    csrf2 = login_as(second_client, conn, tk2)
    create_request(second_client, csrf2, m2)

    resp = second_client.get("/api/v1/unrestriction-requests")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["total_count"] == 1
    assert body["results"][0]["member_id"] == m2


def test_list_admin_sees_everyone(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    create_request(client, tk_csrf, member)

    login_as(second_client, conn, admin_id)
    resp = second_client.get("/api/v1/unrestriction-requests")
    assert resp.get_json()["total_count"] == 1


def test_list_rejects_invalid_status_filter(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/unrestriction-requests?status=bogus")
    assert resp.status_code == 400


def test_list_admin_mine_filter_narrows_to_own_filings(client, second_client, conn, department_id, tool_keeper_id):
    """?mine=1 only means something for an admin/in_charge who USED to be a
    tool_keeper: filing is tool_keeper-only (@roles_required), so the only
    way requested_by_staff_id ever points at an admin/in_charge account is a
    later role promotion - their old filings stay attributed to the same
    staff row. Simulated here by filing as tool_keeper, then promoting that
    same staff row to admin."""
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    create_request(client, tk_csrf, member)

    # A second, unrelated filing by someone else - proves ?mine=1 actually narrows.
    other_target = make_member(conn, department_id)
    restrict(conn, other_target)
    other_tk = make_staff(conn, department_id, "tool_keeper", "other_tk")
    other_csrf = login_as(second_client, conn, other_tk)
    create_request(second_client, other_csrf, other_target)

    conn.execute("UPDATE staff SET role = 'admin' WHERE id = ?", (tool_keeper_id,))
    conn.commit()
    promoted_csrf = login_as(client, conn, tool_keeper_id)  # fresh session picks up the new role

    resp = client.get("/api/v1/unrestriction-requests?mine=1", headers={"X-CSRF-Token": promoted_csrf})
    assert resp.get_json()["total_count"] == 1


# # # APPROVE / DENY (admin/in_charge only) ------------------------------------


def test_approve_forbidden_for_tool_keeper(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    req_id = create_request(client, csrf, member_id).get_json()["id"]
    resp = client.post(f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


def test_approve_success_lifts_the_restriction(
    client, second_client, conn, department_id, tool_keeper_id, admin_id
):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 200

    row = conn.execute("SELECT is_restricted, restriction_reason FROM members WHERE id = ?", (member,)).fetchone()
    assert row["is_restricted"] == 0
    assert row["restriction_reason"] is None

    req_row = conn.execute("SELECT status FROM unrestriction_requests WHERE id = ?", (req_id,)).fetchone()
    assert req_row["status"] == "approved"


def test_approve_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/unrestriction-requests/999999/approve", json={}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 404


def test_approve_already_reviewed_is_rejected(
    client, second_client, conn, department_id, tool_keeper_id, admin_id
):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    second_client.post(f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": admin_csrf})

    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 409


def test_approve_rejects_self_approval(client, second_client, conn, department_id, admin_id, tool_keeper_id):
    """An admin/in_charge can't approve an unrestriction request filed for
    their own member account - must be a different reviewer."""
    admin_member_id = conn.execute("SELECT member_id FROM staff WHERE id = ?", (admin_id,)).fetchone()[
        "member_id"
    ]
    restrict(conn, admin_member_id)

    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, admin_member_id).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 403


def test_deny_requires_review_notes(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/deny", json={}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 400


def test_deny_success_leaves_member_restricted(
    client, second_client, conn, department_id, tool_keeper_id, admin_id
):
    member = make_member(conn, department_id)
    restrict(conn, member)
    tk_csrf = login_as(client, conn, tool_keeper_id)
    req_id = create_request(client, tk_csrf, member).get_json()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        f"/api/v1/unrestriction-requests/{req_id}/deny",
        json={"review_notes": "Not resolved yet"},
        headers={"X-CSRF-Token": admin_csrf},
    )
    assert resp.status_code == 200

    row = conn.execute("SELECT is_restricted FROM members WHERE id = ?", (member,)).fetchone()
    assert row["is_restricted"] == 1

    req_row = conn.execute("SELECT status FROM unrestriction_requests WHERE id = ?", (req_id,)).fetchone()
    assert req_row["status"] == "denied"


def test_deny_forbidden_for_tool_keeper(as_tool_keeper, conn, member_id):
    client, csrf, _ = as_tool_keeper
    restrict(conn, member_id)
    req_id = create_request(client, csrf, member_id).get_json()["id"]
    resp = client.post(
        f"/api/v1/unrestriction-requests/{req_id}/deny",
        json={"review_notes": "no"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 403
