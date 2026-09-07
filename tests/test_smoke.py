"""Sanity checks for the test infrastructure itself (tests/conftest.py) --
if these fail, every other test file's fixtures are suspect too."""


def test_fresh_db_has_real_schema(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    for expected in ("staff", "members", "assets", "transactions", "checkout_sessions", "sessions"):
        assert expected in tables


def test_department_member_staff_asset_fixtures_are_isolated(department_id, member_id, admin_id, asset_id):
    assert department_id > 0
    assert member_id > 0
    assert admin_id > 0
    assert asset_id > 0


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.get_json()["status"] == "ok"


def test_unauthenticated_request_is_rejected(client):
    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 401


def test_login_fixture_authenticates(as_admin):
    client, csrf, admin_id = as_admin
    resp = client.get("/api/v1/transactions")
    assert resp.status_code == 200


def test_two_tests_do_not_share_a_database(conn):
    """migrations/0046 seeds 10 named departments on every fresh DB - if
    fresh_db weren't actually isolating per test, this count would include
    the extra department test_department_member_staff_asset_fixtures_are_
    isolated (above) created too."""
    count = conn.execute("SELECT COUNT(*) AS c FROM departments").fetchone()["c"]
    assert count == 10
    names = {row["name"] for row in conn.execute("SELECT name FROM departments").fetchall()}
    assert "Electrical Installation and Maintenance" not in names
