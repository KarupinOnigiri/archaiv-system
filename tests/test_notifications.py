"""The notification bell / Notifications tab - routes/notifications.py +
services/notification_service.py.

Uses the unrestriction-request flow (tests/test_requests.py) as a realistic
way to generate real notification rows - filing one broadcasts a
'unrestriction_requested' notification to both the admin and in_charge
roles (create_unrestriction_requested_notifications), which is simpler and
more representative than hand-inserting rows into `notifications` with the
right (type, recipient_role/recipient_staff_id) shape.

Almost every test here needs two DIFFERENT staff acting (one files, another
reads/clears) - `client` + `second_client` (tests/conftest.py), never two
`as_<role>` fixtures together (they share one cookie jar - see
test_requests.py's module docstring for why that silently breaks)."""

from tests.conftest import login_as, make_member, make_staff


def restrict(conn, member_id):
    conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_reason = 'x', restriction_source = 'manual' WHERE id = ?",
        (member_id,),
    )
    conn.commit()


def file_a_request(client, csrf, member_id):
    """Broadcasts one 'unrestriction_requested' notification to admin AND
    in_charge (two separate rows, recipient_role-scoped - see
    notification_service.create_unrestriction_requested_notifications)."""
    resp = client.post(
        "/api/v1/unrestriction-requests",
        json={"member_id": member_id, "reason": "test"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["id"]


def seed_notification(client, csrf, conn, department_id):
    """tool_keeper files a request against a fresh restricted member --
    returns the id of the notification visible to admin/in_charge."""
    member = make_member(conn, department_id)
    restrict(conn, member)
    file_a_request(client, csrf, member)
    notif = conn.execute(
        "SELECT id FROM notifications WHERE recipient_role = 'admin' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return notif["id"]


# # # VISIBILITY ---------------------------------------------------------------


def test_role_targeted_notification_visible_to_admin(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    seed_notification(client, tk_csrf, conn, department_id)

    login_as(second_client, conn, admin_id)
    resp = second_client.get("/api/v1/notifications/table")
    assert resp.status_code == 200
    assert b"requested unrestriction" in resp.data


def test_role_targeted_notification_not_visible_to_uninvolved_role(
    client, second_client, conn, department_id, tool_keeper_id
):
    """tool_keeper isn't a recipient of admin/in_charge-targeted
    notifications - a *different* tool_keeper (not the filer) should see
    none of them."""
    tk_csrf = login_as(client, conn, tool_keeper_id)
    seed_notification(client, tk_csrf, conn, department_id)

    other_tk = make_staff(conn, department_id, "tool_keeper", "other_tk")
    login_as(second_client, conn, other_tk)
    resp = second_client.get("/api/v1/notifications/table")
    assert b"requested unrestriction" not in resp.data


def test_notifications_table_requires_login(client):
    resp = client.get("/api/v1/notifications/table")
    assert resp.status_code == 401


def test_notifications_table_rejects_invalid_since(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/notifications/table?since=not-a-date")
    assert resp.status_code == 400


def test_notifications_table_show_unread_only(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    notif_id = seed_notification(client, tk_csrf, conn, department_id)

    admin_csrf = login_as(second_client, conn, admin_id)
    second_client.post(f"/api/v1/notifications/{notif_id}/mark-read", headers={"X-CSRF-Token": admin_csrf})

    resp = second_client.get("/api/v1/notifications/table?show_unread_only=1")
    assert b"requested unrestriction" not in resp.data


# # # MARK READ -----------------------------------------------------------------


def test_mark_read_only_affects_the_marking_staff_member(
    client, second_client, conn, department_id, tool_keeper_id, admin_id, in_charge_id
):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    notif_id = seed_notification(client, tk_csrf, conn, department_id)

    admin_csrf = login_as(client, conn, admin_id)
    resp = client.post(f"/api/v1/notifications/{notif_id}/mark-read", headers={"X-CSRF-Token": admin_csrf})
    assert resp.status_code == 200

    # in_charge's own copy is untouched - still shows up under unread-only.
    in_charge_csrf = login_as(second_client, conn, in_charge_id)
    resp2 = second_client.get(
        "/api/v1/notifications/table?show_unread_only=1", headers={"X-CSRF-Token": in_charge_csrf}
    )
    assert b"requested unrestriction" in resp2.data


def test_mark_read_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/notifications/999999/mark-read", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 404


def test_mark_read_not_visible_to_this_staff_is_404(client, second_client, conn, department_id):
    """A tool_keeper trying to mark an admin/in_charge-only notification
    read shouldn't be able to address it at all - 404, not a silent no-op."""
    tk1 = make_staff(conn, department_id, "tool_keeper", "tk1")
    tk1_csrf = login_as(client, conn, tk1)
    notif_id = seed_notification(client, tk1_csrf, conn, department_id)

    tk2 = make_staff(conn, department_id, "tool_keeper", "tk2")
    tk2_csrf = login_as(second_client, conn, tk2)
    resp = second_client.post(f"/api/v1/notifications/{notif_id}/mark-read", headers={"X-CSRF-Token": tk2_csrf})
    assert resp.status_code == 404


def test_mark_all_read(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    seed_notification(client, tk_csrf, conn, department_id)
    m2 = make_member(conn, department_id)
    restrict(conn, m2)
    file_a_request(client, tk_csrf, m2)

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post("/api/v1/notifications/mark-all-read", headers={"X-CSRF-Token": admin_csrf})
    assert resp.status_code == 200

    unread = second_client.get("/api/v1/notifications/table?show_unread_only=1").data
    assert b"requested unrestriction" not in unread


# # # CLEAR ---------------------------------------------------------------------


def test_clear_single_notification(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    notif_id = seed_notification(client, tk_csrf, conn, department_id)

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(f"/api/v1/notifications/{notif_id}/clear", headers={"X-CSRF-Token": admin_csrf})
    assert resp.status_code == 200

    listing = second_client.get("/api/v1/notifications/table").data
    assert b"requested unrestriction" not in listing


def test_clear_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/notifications/999999/clear", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 404


def test_clear_bulk(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    n1 = seed_notification(client, tk_csrf, conn, department_id)
    m2 = make_member(conn, department_id)
    restrict(conn, m2)
    file_a_request(client, tk_csrf, m2)
    n2 = conn.execute(
        "SELECT id FROM notifications WHERE recipient_role='admin' ORDER BY id DESC LIMIT 1"
    ).fetchone()["id"]

    admin_csrf = login_as(second_client, conn, admin_id)
    resp = second_client.post(
        "/api/v1/notifications/clear", json={"ids": [n1, n2]}, headers={"X-CSRF-Token": admin_csrf}
    )
    assert resp.status_code == 200
    assert resp.get_json()["cleared_count"] == 2


def test_clear_bulk_rejects_empty_ids(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/notifications/clear", json={"ids": []}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_clear_bulk_rejects_non_integer_ids(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/notifications/clear", json={"ids": ["not-an-int"]}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 400


def test_clear_bulk_silently_skips_ids_not_visible_to_this_staff(client, second_client, conn, department_id):
    tk1 = make_staff(conn, department_id, "tool_keeper", "tk1")
    tk1_csrf = login_as(client, conn, tk1)
    notif_id = seed_notification(client, tk1_csrf, conn, department_id)

    tk2 = make_staff(conn, department_id, "tool_keeper", "tk2")
    tk2_csrf = login_as(second_client, conn, tk2)
    resp = second_client.post(
        "/api/v1/notifications/clear", json={"ids": [notif_id]}, headers={"X-CSRF-Token": tk2_csrf}
    )
    assert resp.status_code == 200
    assert resp.get_json()["cleared_count"] == 0


# # # OPEN (mark-read + redirect) -----------------------------------------------


def test_open_unrestriction_notification_redirects_to_requests(
    client, second_client, conn, department_id, tool_keeper_id, admin_id
):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    notif_id = seed_notification(client, tk_csrf, conn, department_id)

    login_as(second_client, conn, admin_id)
    resp = second_client.get(f"/api/v1/notifications/{notif_id}/open", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/requests")


def test_open_marks_it_read(client, second_client, conn, department_id, tool_keeper_id, admin_id):
    tk_csrf = login_as(client, conn, tool_keeper_id)
    notif_id = seed_notification(client, tk_csrf, conn, department_id)

    login_as(second_client, conn, admin_id)
    second_client.get(f"/api/v1/notifications/{notif_id}/open")

    unread = second_client.get("/api/v1/notifications/table?show_unread_only=1").data
    assert b"requested unrestriction" not in unread


def test_open_not_found_falls_back_to_notifications_page(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/notifications/999999/open", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/notifications")
