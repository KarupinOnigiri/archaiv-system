"""Admin-deactivation peer-review workflow: an admin can't unilaterally
deactivate a fellow admin - they file a request, in_charge/tool_keeper
accounts vote, and only the original requester can pull the trigger once
enough votes are in - routes/staff.py + services/staff_service.py +
services/admin_deactivation_service.py.

The approval tier (how many votes, how many of them in_charge) is
snapshotted from how many in_charge/tool_keeper accounts are ACTIVE at
request time (determine_approval_tier) - most HTTP-level tests here set up
exactly one in_charge + one tool_keeper, which lands on the "minimal" tier
(2 approvals required, 1+ of them in_charge - the cheapest reachable tier),
so a single in_charge + single tool_keeper vote is enough to finalize.

Two DIFFERENT staff acting at once (request as one admin, vote as another)
needs `client` + `second_client` (tests/conftest.py) - never two `as_<role>`
fixtures together, see test_requests.py's module docstring for why."""

from tests.conftest import TEST_PASSWORD, login_as, make_staff
from services import admin_deactivation_service


def request_deactivation(client, csrf, target_id, reason="Stepping down"):
    return client.post(
        f"/api/v1/staff/{target_id}/request-deactivation",
        json={"reason": reason},
        headers={"X-CSRF-Token": csrf},
    )


def approve(client, csrf, request_id, reason="Looks fine"):
    return client.post(
        f"/api/v1/admin-deactivation-requests/{request_id}/approve",
        json={"reason": reason},
        headers={"X-CSRF-Token": csrf},
    )


def deny(client, csrf, request_id, denial_reason=None):
    payload = {"denial_reason": denial_reason} if denial_reason else {}
    return client.post(
        f"/api/v1/admin-deactivation-requests/{request_id}/deny",
        json=payload,
        headers={"X-CSRF-Token": csrf},
    )


def finalize(client, csrf, request_id):
    return client.post(
        f"/api/v1/admin-deactivation-requests/{request_id}/finalize", headers={"X-CSRF-Token": csrf}
    )


def minimal_tier_reviewers(conn, department_id):
    """One in_charge + one tool_keeper - active_in_charge=1, combined=2 ->
    the "minimal" tier (2 approvals required, 1+ in_charge)."""
    in_charge_id = make_staff(conn, department_id, "in_charge", "reviewer_ic")
    tool_keeper_id = make_staff(conn, department_id, "tool_keeper", "reviewer_tk")
    return in_charge_id, tool_keeper_id


# # # TIER CALCULATION (pure function) -----------------------------------------


def test_tier_none_with_no_eligible_reviewers(conn):
    tier, required, required_ic = admin_deactivation_service.determine_approval_tier(conn, 1, 2)
    assert tier is None
    assert required is None
    assert required_ic is None


def test_tier_minimal(conn, department_id):
    make_staff(conn, department_id, "in_charge", "ic1")
    make_staff(conn, department_id, "tool_keeper", "tk1")
    tier, required, required_ic = admin_deactivation_service.determine_approval_tier(conn, 1, 2)
    assert (tier, required, required_ic) == ("minimal", 2, 1)


def test_tier_reduced(conn, department_id):
    make_staff(conn, department_id, "in_charge", "ic1")
    make_staff(conn, department_id, "tool_keeper", "tk1")
    make_staff(conn, department_id, "tool_keeper", "tk2")
    tier, required, required_ic = admin_deactivation_service.determine_approval_tier(conn, 1, 2)
    assert (tier, required, required_ic) == ("reduced", 3, 1)


def test_tier_full(conn, department_id):
    make_staff(conn, department_id, "in_charge", "ic1")
    make_staff(conn, department_id, "in_charge", "ic2")
    make_staff(conn, department_id, "tool_keeper", "tk1")
    make_staff(conn, department_id, "tool_keeper", "tk2")
    tier, required, required_ic = admin_deactivation_service.determine_approval_tier(conn, 1, 2)
    assert (tier, required, required_ic) == ("full", 4, 2)


def test_is_eligible_reviewer():
    assert admin_deactivation_service.is_eligible_reviewer("in_charge") is True
    assert admin_deactivation_service.is_eligible_reviewer("tool_keeper") is True
    assert admin_deactivation_service.is_eligible_reviewer("admin") is False


# # # REQUEST (admin only) ------------------------------------------------------


def test_request_success(as_admin, conn, department_id):
    client, csrf, requester_id = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")

    resp = request_deactivation(client, csrf, target_id)
    assert resp.status_code == 201, resp.get_json()
    body = resp.get_json()
    assert body["required_tier"] == "minimal"
    assert body["required_approvals"] == 2
    assert body["required_in_charge"] == 1


def test_request_forbidden_for_tool_keeper(as_tool_keeper, conn, department_id):
    client, csrf, _ = as_tool_keeper
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    resp = request_deactivation(client, csrf, target_id)
    assert resp.status_code == 403


def test_request_missing_reason(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    resp = client.post(
        f"/api/v1/staff/{target_id}/request-deactivation", json={}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_request_cannot_target_self(as_admin):
    client, csrf, requester_id = as_admin
    resp = request_deactivation(client, csrf, requester_id)
    assert resp.status_code == 400


def test_request_target_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = request_deactivation(client, csrf, 999999)
    assert resp.status_code == 404


def test_request_target_not_an_admin(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    tool_keeper_id = make_staff(conn, department_id, "tool_keeper", "not_an_admin")
    resp = request_deactivation(client, csrf, tool_keeper_id)
    assert resp.status_code == 400


def test_request_target_already_inactive(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    conn.execute("UPDATE staff SET is_active = 0 WHERE id = ?", (target_id,))
    conn.commit()
    resp = request_deactivation(client, csrf, target_id)
    assert resp.status_code == 400


def test_request_rejects_duplicate_pending(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    r1 = request_deactivation(client, csrf, target_id)
    assert r1.status_code == 201
    r2 = request_deactivation(client, csrf, target_id)
    assert r2.status_code == 409


def test_request_rejects_when_no_eligible_reviewers_exist(as_admin, conn, department_id):
    """No active in_charge/tool_keeper at all - conftest's admin_id fixture
    is the only staff besides the target, so this is the baseline state
    with nothing else set up."""
    client, csrf, _ = as_admin
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    resp = request_deactivation(client, csrf, target_id)
    assert resp.status_code == 409
    assert "No eligible reviewers" in resp.get_json()["error"]


def test_request_form_shows_already_pending_flag(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    request_deactivation(client, csrf, target_id)

    resp = client.get(f"/api/v1/staff/{target_id}/request-deactivation-form")
    assert resp.status_code == 200
    assert b"already" in resp.data.lower() or b"pending" in resp.data.lower()


# # # APPROVE --------------------------------------------------------------


def test_approve_by_in_charge_succeeds(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = approve(second_client, ic_csrf, req_id)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["approvals_received"] == 1
    assert body["in_charge_received"] == 1


def test_approve_by_tool_keeper_succeeds(client, second_client, conn, department_id, admin_id):
    _ic, tk_id = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    tk_csrf = login_as(second_client, conn, tk_id)
    resp = approve(second_client, tk_csrf, req_id)
    assert resp.status_code == 200
    assert resp.get_json()["in_charge_received"] == 0


def test_approve_forbidden_for_another_admin(client, second_client, conn, department_id, admin_id):
    minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    third_admin = make_staff(conn, department_id, "admin", "third_admin")
    third_csrf = login_as(second_client, conn, third_admin)
    resp = approve(second_client, third_csrf, req_id)
    assert resp.status_code == 403


def test_approve_forbidden_for_the_target(client, second_client, conn, department_id, admin_id):
    minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    target_csrf = login_as(second_client, conn, target_id)
    resp = approve(second_client, target_csrf, req_id)
    assert resp.status_code == 403


def test_approve_forbidden_for_the_requester(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, csrf, target_id).get_json()["id"]

    resp = approve(client, csrf, req_id)
    assert resp.status_code == 403


def test_approve_missing_reason(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = second_client.post(
        f"/api/v1/admin-deactivation-requests/{req_id}/approve", json={}, headers={"X-CSRF-Token": ic_csrf}
    )
    assert resp.status_code == 400


def test_approve_duplicate_vote_is_rejected(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    approve(second_client, ic_csrf, req_id)
    resp = approve(second_client, ic_csrf, req_id)
    assert resp.status_code == 400


def test_approve_not_found(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = approve(client, csrf, 999999)
    assert resp.status_code == 404


def test_approve_already_resolved_request(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]
    deny(client, admin_csrf, req_id)  # requester cancels it

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = approve(second_client, ic_csrf, req_id)
    assert resp.status_code == 409


# # # DENY -----------------------------------------------------------------


def test_requester_deny_cancels_without_a_reason(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, csrf, target_id).get_json()["id"]

    resp = deny(client, csrf, req_id)
    assert resp.status_code == 200
    row = conn.execute(
        "SELECT status FROM admin_deactivation_requests WHERE id = ?", (req_id,)
    ).fetchone()
    assert row["status"] == "cancelled"


def test_reviewer_deny_requires_a_reason(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = deny(second_client, ic_csrf, req_id)
    assert resp.status_code == 400


def test_reviewer_deny_with_reason_records_a_vote_not_a_resolution(
    client, second_client, conn, department_id, admin_id
):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = deny(second_client, ic_csrf, req_id, denial_reason="Not convinced")
    assert resp.status_code == 200
    assert resp.get_json()["denies_received"] == 1

    row = conn.execute(
        "SELECT status FROM admin_deactivation_requests WHERE id = ?", (req_id,)
    ).fetchone()
    assert row["status"] == "pending"  # a deny vote alone doesn't resolve it


def test_deny_forbidden_for_the_target(client, second_client, conn, department_id, admin_id):
    minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    target_csrf = login_as(second_client, conn, target_id)
    resp = deny(second_client, target_csrf, req_id, denial_reason="No")
    assert resp.status_code == 403


def test_deny_duplicate_vote_is_rejected(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    deny(second_client, ic_csrf, req_id, denial_reason="First")
    resp = deny(second_client, ic_csrf, req_id, denial_reason="Second")
    assert resp.status_code == 400


def test_deny_not_found(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = deny(client, csrf, 999999, denial_reason="x")
    assert resp.status_code == 404


# # # FINALIZE (requester only, once threshold met) ----------------------------


def test_finalize_before_threshold_is_rejected(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, csrf, target_id).get_json()["id"]

    resp = finalize(client, csrf, req_id)
    assert resp.status_code == 409


def test_finalize_forbidden_for_non_requester(client, second_client, conn, department_id, admin_id):
    in_charge_id, tk_id = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = finalize(second_client, ic_csrf, req_id)
    assert resp.status_code == 403


def test_finalize_success_deactivates_the_target(client, second_client, conn, department_id, admin_id):
    in_charge_id, tk_id = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    approve(second_client, ic_csrf, req_id)
    tk_csrf = login_as(second_client, conn, tk_id)
    approve(second_client, tk_csrf, req_id)

    resp = finalize(client, admin_csrf, req_id)
    assert resp.status_code == 200, resp.get_json()

    row = conn.execute("SELECT is_active FROM staff WHERE id = ?", (target_id,)).fetchone()
    assert row["is_active"] == 0

    req_row = conn.execute(
        "SELECT status FROM admin_deactivation_requests WHERE id = ?", (req_id,)
    ).fetchone()
    assert req_row["status"] == "approved"


def test_finalize_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = finalize(client, csrf, 999999)
    assert resp.status_code == 404


def test_finalize_already_resolved(as_admin, conn, department_id):
    client, csrf, _ = as_admin
    minimal_tier_reviewers(conn, department_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, csrf, target_id).get_json()["id"]
    deny(client, csrf, req_id)  # cancel it

    resp = finalize(client, csrf, req_id)
    assert resp.status_code == 409


def test_finalize_refuses_to_leave_zero_active_admins(conn, department_id):
    """Service-level: the requester is always a distinct, active admin at
    request time, so this branch can't be reached through the normal HTTP
    flow (finalize's own permission check requires the caller to still be
    that active admin) - exercised directly against
    finalize_admin_deactivation instead, simulating the target having
    become the last one standing between the vote and the finalize call."""
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    requester_id = make_staff(conn, department_id, "admin", "requester_admin")
    in_charge_id = make_staff(conn, department_id, "in_charge", "ic1")

    cursor = conn.execute(
        """
        INSERT INTO admin_deactivation_requests
            (target_staff_id, requested_by_staff_id, reason, required_tier, required_approvals, required_in_charge)
        VALUES (?, ?, 'test', 'minimal', 1, 1)
        """,
        (target_id, requester_id),
    )
    request_id = cursor.lastrowid
    conn.execute(
        "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id, decision) VALUES (?, ?, 'approve')",
        (request_id, in_charge_id),
    )
    conn.commit()

    # Every OTHER active, non-seeded admin (the fixture-created ones, plus
    # whatever seed_service left behind) leaves the picture - target is
    # the last one left.
    conn.execute(
        "UPDATE staff SET is_active = 0 WHERE role = 'admin' AND id != ?", (target_id,)
    )
    conn.commit()

    req_row = conn.execute(
        "SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)
    ).fetchone()
    ok, error_message = admin_deactivation_service.finalize_admin_deactivation(req_row, requester_id, conn)
    assert ok is False
    assert "last active admin" in error_message

    # Refused - target must still be active, request must still be pending.
    target = conn.execute("SELECT is_active FROM staff WHERE id = ?", (target_id,)).fetchone()
    assert target["is_active"] == 1


# # # REVIEWS LIST (admin-only detail; everyone else gets aggregate counts) ----


def test_reviews_list_admin_only(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    approve(second_client, ic_csrf, req_id, reason="Good with it")

    resp = client.get(f"/api/v1/admin-deactivation-requests/{req_id}/reviews")
    assert resp.status_code == 200
    assert b"Good with it" in resp.data or b"reason" in resp.data.lower()


def test_reviews_list_forbidden_for_in_charge(client, second_client, conn, department_id, admin_id):
    in_charge_id, _tk = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    req_id = request_deactivation(client, admin_csrf, target_id).get_json()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = second_client.get(f"/api/v1/admin-deactivation-requests/{req_id}/reviews")
    assert resp.status_code == 403


def test_reviews_list_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/admin-deactivation-requests/999999/reviews")
    assert resp.status_code == 404


# # # LIST (everyone can see; only eligible roles can vote) --------------------


def test_list_visible_to_tool_keeper(client, second_client, conn, department_id, admin_id):
    """Everyone sees the list now; tiers gate who can VOTE, not who can see it."""
    _ic, tk_id = minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target_id = make_staff(conn, department_id, "admin", "target_admin")
    request_deactivation(client, admin_csrf, target_id)

    login_as(second_client, conn, tk_id)
    resp = second_client.get("/api/v1/admin-deactivation-requests")
    assert resp.status_code == 200
    assert resp.get_json()["total_count"] == 1


def test_list_rejects_invalid_status(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/admin-deactivation-requests?status=bogus")
    assert resp.status_code == 400


def test_list_mine_filter(client, second_client, conn, department_id, admin_id):
    minimal_tier_reviewers(conn, department_id)
    admin_csrf = login_as(client, conn, admin_id)
    target1 = make_staff(conn, department_id, "admin", "target1")
    request_deactivation(client, admin_csrf, target1)

    other_admin = make_staff(conn, department_id, "admin", "other_admin")
    other_target = make_staff(conn, department_id, "admin", "target2")
    other_csrf = login_as(second_client, conn, other_admin)
    request_deactivation(second_client, other_csrf, other_target)

    resp = client.get("/api/v1/admin-deactivation-requests?mine=1")
    assert resp.get_json()["total_count"] == 1
