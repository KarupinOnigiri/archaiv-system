import database
from tests.conftest import make_member, make_staff_with_password, login, DEFAULT_TEST_PASSWORD
from concurrent.futures import ThreadPoolExecutor


def make_asset(conn, barcode):
    cur = conn.execute(
        "INSERT INTO assets (asset_id, name, barcode_id) VALUES ('', 'Drill', ?)",
        (barcode,),
    )
    conn.commit()
    return cur.lastrowid


def test_two_carts_cannot_claim_same_asset_concurrently(client, db_conn):
    _, username = make_staff_with_password(db_conn, "admin", "ADM-RACE-1")
    csrf = login(client, username, DEFAULT_TEST_PASSWORD)

    asset_id = make_asset(db_conn, "AST-RACE-2")
    member_a = make_member(db_conn, "MEM-A")
    member_b = make_member(db_conn, "MEM-B")

    session_a = db_conn.execute(
        "INSERT INTO checkout_sessions (member_id) VALUES (?)", (member_a,)
    ).lastrowid
    session_b = db_conn.execute(
        "INSERT INTO checkout_sessions (member_id) VALUES (?)", (member_b,)
    ).lastrowid
    db_conn.commit()

    # Flask's test client keeps its request/app context on a contextvar
    # stack that isn't safe to drive from two threads at once -- sharing
    # the `client` fixture directly here races on push/pop and raises
    # LookupError('flask.app_ctx') under load. Each thread gets its own
    # throwaway client instead, carrying over the same session cookie (and
    # therefore the same CSRF token) so both still hit the endpoint as the
    # one logged-in staff member this test set up above.
    session_cookie = client.get_cookie("session_token")

    def add_to_session(session_id):
        thread_client = client.application.test_client()
        thread_client.set_cookie("session_token", session_cookie.value)
        return thread_client.post(
            f"/api/v1/checkout-sessions/{session_id}/items/add",
            json={"asset_code": "AST-RACE-2"},
            headers={"X-CSRF-Token": csrf},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = pool.map(add_to_session, [session_a, session_b])

    statuses = sorted([r1.status_code, r2.status_code])
    assert (
        statuses[0] == 201
    ), f"expected exactly one request to succeed, got {statuses}"
    assert statuses[1] in (
        400,
        409,
    ), f"expected the losing request to be rejected (400 or 409), got {statuses}"


def make_asset(conn, barcode):
    cur = conn.execute(
        "INSERT INTO assets (asset_id, name, barcode_id) VALUES ('', 'Drill', ?)",
        (barcode,),
    )
    conn.commit()
    return cur.lastrowid


def test_conditional_update_prevents_double_claim(db_conn):
    asset_id = make_asset(db_conn, "AST-RACE-1")

    conn_a = database.get_db_connection()
    conn_b = database.get_db_connection()

    claim_a = conn_a.execute(
        "UPDATE assets SET status = 'reserved' WHERE id = ? AND status = 'available'",
        (asset_id,),
    )
    conn_a.commit()

    claim_b = conn_b.execute(
        "UPDATE assets SET status = 'reserved' WHERE id = ? AND status = 'available'",
        (asset_id,),
    )
    conn_b.commit()

    assert claim_a.rowcount == 1
    assert claim_b.rowcount == 0  # second claimant correctly gets nothing

    conn_a.close()
    conn_b.close()
