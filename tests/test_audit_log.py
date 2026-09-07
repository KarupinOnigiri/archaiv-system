"""Activity Log: every staff member sees their own audit trail; only admin
can filter to (or see) someone else's - routes/staff.py's audit-log
endpoints + services/staff_service.get_audit_log_context + the page shell
in routes/pages.py.

log_action() (utils/logger.py) fires on nearly every state-changing route
already exercised elsewhere in this suite, so rather than hand-inserting
audit_log rows, most tests here just do one real action (e.g. resolving
maintenance) and check it shows up correctly scoped."""

from tests.conftest import login_as, make_asset, make_staff


def do_something_as(client, csrf, asset_id):
    """One real audited action: resolve-maintenance quick action."""
    conn_action = client.post(
        f"/api/v1/assets/{asset_id}/resolve-maintenance",
        json={"condition": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert conn_action.status_code == 200, conn_action.get_json()


# # # API: /api/v1/audit-log ----------------------------------------------


def test_requires_login(client):
    resp = client.get("/api/v1/audit-log")
    assert resp.status_code == 401


def test_non_admin_only_sees_their_own_entries(
    client, second_client, conn, department_id, admin_id, in_charge_id
):
    admin_csrf = login_as(client, conn, admin_id)
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, admin_csrf, asset)

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = second_client.get("/api/v1/audit-log")
    assert resp.status_code == 200
    # in_charge never did anything - their own-scoped log is empty, even
    # though the admin's action above is in the table.
    assert resp.get_json()["total_count"] == 0


def test_non_admin_cannot_escape_their_own_scope_via_staff_id_param(
    client, second_client, conn, department_id, admin_id, in_charge_id
):
    """A non-admin passing ?staff_id=<someone else> is silently ignored --
    get_audit_log_context forces the filter to their own id regardless."""
    admin_csrf = login_as(client, conn, admin_id)
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, admin_csrf, asset)

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = second_client.get(f"/api/v1/audit-log?staff_id={admin_id}")
    assert resp.get_json()["total_count"] == 0


def test_admin_sees_everyone_and_can_filter_by_staff_id(
    client, second_client, conn, department_id, admin_id, in_charge_id
):
    admin_csrf = login_as(client, conn, admin_id)
    asset1 = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset1,))
    conn.commit()
    do_something_as(client, admin_csrf, asset1)

    ic_csrf = login_as(second_client, conn, in_charge_id)
    asset2 = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset2,))
    conn.commit()
    do_something_as(second_client, ic_csrf, asset2)

    login_as(client, conn, admin_id)
    resp_all = client.get("/api/v1/audit-log")
    assert resp_all.get_json()["total_count"] >= 2

    resp_scoped = client.get(f"/api/v1/audit-log?staff_id={in_charge_id}")
    body = resp_scoped.get_json()
    assert body["total_count"] == 1
    assert body["results"][0]["staff_id"] == in_charge_id


def test_filter_by_action(as_admin, conn):
    client, csrf, _ = as_admin
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, csrf, asset)

    resp = client.get("/api/v1/audit-log?action=resolved_maintenance")
    assert resp.get_json()["total_count"] >= 1

    resp2 = client.get("/api/v1/audit-log?action=nonexistent_action")
    assert resp2.get_json()["total_count"] == 0


def test_filter_by_target_table(as_admin, conn):
    client, csrf, _ = as_admin
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, csrf, asset)

    resp = client.get("/api/v1/audit-log?target_table=assets")
    assert resp.get_json()["total_count"] >= 1


def test_rejects_invalid_since(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/audit-log?since=not-a-date")
    assert resp.status_code == 400


def test_rejects_invalid_until(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/audit-log?until=not-a-date")
    assert resp.status_code == 400


def test_until_is_inclusive_of_the_whole_day(as_admin, conn):
    """until compares against created_at (a full timestamp) - get_audit_log_
    context deliberately compares against the *next* day's start so an
    entry logged any time today still matches ?until=today, not just
    entries at exactly midnight."""
    from datetime import date

    client, csrf, _ = as_admin
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, csrf, asset)

    today = date.today().isoformat()
    resp = client.get(f"/api/v1/audit-log?until={today}")
    assert resp.get_json()["total_count"] >= 1


def test_pagination(as_admin, conn, category_id):
    client, csrf, _ = as_admin
    for _ in range(3):
        asset = make_asset(conn, category_id)
        conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
        conn.commit()
        do_something_as(client, csrf, asset)

    resp = client.get("/api/v1/audit-log?limit=2&offset=0")
    body = resp.get_json()
    assert len(body["results"]) == 2
    assert body["total_count"] >= 3


# # # HTMX PARTIAL: /api/v1/audit-log/table --------------------------------


def test_table_partial_requires_login(client):
    resp = client.get("/api/v1/audit-log/table")
    assert resp.status_code == 401


def test_table_partial_renders(as_admin, conn):
    client, csrf, _ = as_admin
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, csrf, asset)

    resp = client.get("/api/v1/audit-log/table")
    assert resp.status_code == 200
    assert b"resolved_maintenance" in resp.data.lower() or b"maintenance" in resp.data.lower()


# # # PAGE SHELL: /audit-log ------------------------------------------------


def test_page_requires_login(client):
    resp = client.get("/audit-log", follow_redirects=False)
    assert resp.status_code == 302


def test_page_loads_for_tool_keeper(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.get("/audit-log")
    assert resp.status_code == 200


def test_page_loads_for_admin_with_staff_dropdown(as_admin, conn, department_id):
    """Admin's page load additionally queries every staff account for the
    filter dropdown - just confirms that extra query path doesn't error
    with more than one staff account in play."""
    client, csrf, _ = as_admin
    make_staff(conn, department_id, "tool_keeper", "dropdown_tk")
    resp = client.get("/audit-log")
    assert resp.status_code == 200


# # # STAFF PROFILE: "Full Activity" modal -----------------------------------
# routes/staff.py:staff_full_activity_modal + templates/staff/_full_activity_modal.html.
# Reuses get_audit_log_context via the same /api/v1/audit-log/table endpoint
# as the page above - regression coverage for a real bug where the modal's
# since/until/staff_id hidden inputs were missing their name= attributes,
# so hx-include (like native form submission) never actually sent them:
# the modal silently showed the latest 20 entries across EVERY staff
# member instead of just the one it claimed to, with no error to notice.


def test_full_activity_modal_hidden_inputs_have_name_attrs(as_admin, conn, department_id):
    """The regression itself: hx-include only sends a field that has a
    real name= attribute, id= alone isn't enough - this is what silently
    broke the staff/date scoping with no visible error."""
    client, csrf, _ = as_admin
    tk_id = make_staff(conn, department_id, "tool_keeper", "full_activity_tk")
    resp = client.get(f"/api/v1/staff/{tk_id}/full-activity-modal")
    assert resp.status_code == 200
    html = resp.get_data(as_text=True)
    assert 'name="staff_id"' in html
    assert 'name="since"' in html
    assert 'name="until"' in html


def test_full_activity_modal_table_is_actually_scoped_to_that_staff(
    as_admin, conn, department_id, admin_id
):
    """The modal's initial table load must return only this staff's own
    entries, matching /api/v1/audit-log?staff_id=<id> exactly - not the
    unfiltered, everyone-mixed-together count the missing name= attrs
    used to silently produce instead."""
    client, csrf, _ = as_admin
    other_staff_id = make_staff(conn, department_id, "in_charge", "full_activity_other")
    other_csrf = login_as(client, conn, other_staff_id)
    asset = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset,))
    conn.commit()
    do_something_as(client, other_csrf, asset)

    # A second, unrelated action by a different staff member - if the
    # modal's staff filter silently no-ops, this entry (and the total
    # audit_log row count) would leak into what it displays.
    admin_csrf = login_as(client, conn, admin_id)
    asset2 = make_asset(conn)
    conn.execute("UPDATE assets SET status = 'maintenance' WHERE id = ?", (asset2,))
    conn.commit()
    do_something_as(client, admin_csrf, asset2)

    scoped_via_modal = client.get(f"/api/v1/staff/{other_staff_id}/full-activity-modal")
    assert scoped_via_modal.status_code == 200

    # The JSON endpoint gives an exact, unambiguous total_count to compare
    # against - what the modal's own htmx-rendered table request resolves
    # to must match this precisely, not just "look different" from the
    # unfiltered count.
    scoped = client.get(f"/api/v1/audit-log?staff_id={other_staff_id}").get_json()
    unfiltered = client.get("/api/v1/audit-log").get_json()

    assert scoped["total_count"] == 1
    assert scoped["results"][0]["staff_id"] == other_staff_id
    # Confirms the two staff members' actions didn't get mixed together -
    # the exact leak this bug caused (unfiltered shows both, scoped only one).
    assert unfiltered["total_count"] > scoped["total_count"]
