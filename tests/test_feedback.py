""""Give feedback" (any staff) + admin review (resolve/reopen) --
routes/feedback.py + services/feedback_service.py.

submit_feedback is a multipart form (it can carry screenshots), not JSON --
client.post(..., data={...}) rather than json={...}."""

from tests.conftest import login_as, make_staff


def submit(client, csrf, category="bug", message="Something's broken", page_path="/assets"):
    return client.post(
        "/api/v1/feedback",
        data={"category": category, "message": message, "page_path": page_path},
        headers={"X-CSRF-Token": csrf},
        content_type="multipart/form-data",
    )


# # # SUBMIT (any signed-in staff) ----------------------------------------


def test_submit_requires_login(client):
    resp = client.post("/api/v1/feedback", data={"category": "bug", "message": "x"})
    assert resp.status_code == 401


def test_submit_success_for_tool_keeper(as_tool_keeper):
    """Open to every role, not just admin/in_charge."""
    client, csrf, _ = as_tool_keeper
    resp = submit(client, csrf)
    assert resp.status_code == 200, resp.get_json()
    assert "id" in resp.get_json() or "message" in resp.get_json()


def test_submit_rejects_invalid_category(as_admin):
    client, csrf, _ = as_admin
    resp = submit(client, csrf, category="not-a-real-category")
    assert resp.status_code == 400


def test_submit_rejects_empty_message(as_admin):
    client, csrf, _ = as_admin
    resp = submit(client, csrf, message="")
    assert resp.status_code == 400


def test_submit_rejects_overlong_message(as_admin):
    client, csrf, _ = as_admin
    resp = submit(client, csrf, message="x" * 4001)
    assert resp.status_code == 400


def test_submit_creates_an_open_report(as_admin, conn):
    client, csrf, admin_id = as_admin
    submit(client, csrf, message="A real bug report")
    row = conn.execute(
        "SELECT status, staff_id FROM feedback_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "open"
    assert row["staff_id"] == admin_id


# # # REVIEW (admin only) --------------------------------------------------


def test_table_forbidden_for_non_admin(as_in_charge):
    client, csrf, _ = as_in_charge
    resp = client.get("/api/v1/feedback/table")
    assert resp.status_code == 403


def test_table_shows_submitted_reports(as_admin):
    client, csrf, _ = as_admin
    submit(client, csrf, message="Visible in the table")
    resp = client.get("/api/v1/feedback/table")
    assert resp.status_code == 200
    assert b"Visible in the table" in resp.data


def test_table_rejects_invalid_status(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/feedback/table?status=bogus")
    assert resp.status_code == 400


def test_table_rejects_invalid_category(as_admin):
    client, csrf, _ = as_admin
    resp = client.get("/api/v1/feedback/table?category=bogus")
    assert resp.status_code == 400


def test_resolve_forbidden_for_non_admin(client, second_client, conn, department_id, admin_id, in_charge_id):
    admin_csrf = login_as(client, conn, admin_id)
    submit(client, admin_csrf)
    report_id = conn.execute("SELECT id FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()["id"]

    ic_csrf = login_as(second_client, conn, in_charge_id)
    resp = second_client.post(
        f"/api/v1/feedback/{report_id}/resolve", json={"response": "fixed"}, headers={"X-CSRF-Token": ic_csrf}
    )
    assert resp.status_code == 403


def test_resolve_requires_a_response(as_admin, conn):
    client, csrf, _ = as_admin
    submit(client, csrf)
    report_id = conn.execute("SELECT id FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()["id"]

    resp = client.post(f"/api/v1/feedback/{report_id}/resolve", json={"response": ""}, headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 400


def test_resolve_success(as_admin, conn):
    client, csrf, admin_id = as_admin
    submit(client, csrf)
    report_id = conn.execute("SELECT id FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()["id"]

    resp = client.post(
        f"/api/v1/feedback/{report_id}/resolve",
        json={"response": "Fixed in the next release"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status, admin_response, resolved_by_staff_id FROM feedback_reports WHERE id = ?", (report_id,)
    ).fetchone()
    assert row["status"] == "resolved"
    assert row["admin_response"] == "Fixed in the next release"
    assert row["resolved_by_staff_id"] == admin_id


def test_resolve_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post(
        "/api/v1/feedback/999999/resolve", json={"response": "x"}, headers={"X-CSRF-Token": csrf}
    )
    assert resp.status_code == 404


def test_reopen_success(as_admin, conn):
    client, csrf, _ = as_admin
    submit(client, csrf)
    report_id = conn.execute("SELECT id FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.post(
        f"/api/v1/feedback/{report_id}/resolve", json={"response": "done"}, headers={"X-CSRF-Token": csrf}
    )

    resp = client.post(f"/api/v1/feedback/{report_id}/reopen", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 200

    row = conn.execute(
        "SELECT status, admin_response, resolved_by_staff_id FROM feedback_reports WHERE id = ?", (report_id,)
    ).fetchone()
    assert row["status"] == "open"
    assert row["admin_response"] is None
    assert row["resolved_by_staff_id"] is None


def test_reopen_not_found(as_admin):
    client, csrf, _ = as_admin
    resp = client.post("/api/v1/feedback/999999/reopen", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 404


def test_reopen_forbidden_for_non_admin(as_tool_keeper):
    client, csrf, _ = as_tool_keeper
    resp = client.post("/api/v1/feedback/1/reopen", headers={"X-CSRF-Token": csrf})
    assert resp.status_code == 403


# # # OPEN-COUNT (feeds the sidebar badge) ---------------------------------


def test_open_feedback_count_via_service(as_admin, conn):
    from routes.feedback import get_open_feedback_count

    client, csrf, _ = as_admin
    submit(client, csrf)
    submit(client, csrf, message="A second one")
    assert get_open_feedback_count(conn) == 2

    report_id = conn.execute("SELECT id FROM feedback_reports ORDER BY id DESC LIMIT 1").fetchone()["id"]
    client.post(
        f"/api/v1/feedback/{report_id}/resolve", json={"response": "done"}, headers={"X-CSRF-Token": csrf}
    )
    assert get_open_feedback_count(conn) == 1
