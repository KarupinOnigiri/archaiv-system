"""Covers the borrow/return ("Transactions") tab's non-checkout-flow
endpoints: returning a borrowed asset (both the row-action and
barcode-scan routes), and the table/stat-cards/overdue listing logic
(status filter, the sort_by whitelist, pagination). Checkout-session
creation itself is already exercised by test_checkout_race_conditions.py;
these tests seed transactions directly in the DB rather than driving the
full multi-step checkout flow, since that's not what's under test here.
"""
import re
from datetime import datetime, timedelta

from tests.conftest import make_asset, make_member, make_staff_with_password, login, DEFAULT_TEST_PASSWORD

PAST = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")
FUTURE = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S")


def make_transaction(
    conn,
    asset_id,
    member_id,
    staff_id,
    status="borrowed",
    due_date=FUTURE,
    time_in=None,
    condition_out="functional",
):
    cur = conn.execute(
        """
        INSERT INTO transactions
            (asset_id, member_id, borrowed_by_staff_id, due_date, condition_out, status, time_in)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (asset_id, member_id, staff_id, due_date, condition_out, status, time_in),
    )
    conn.commit()
    return cur.lastrowid


def login_as_admin(client, db_conn, barcode="ADM-TXN"):
    staff_id, username = make_staff_with_password(db_conn, "admin", barcode)
    csrf = login(client, username, DEFAULT_TEST_PASSWORD)
    return staff_id, csrf


# # # Return flow: /transactions/<id>/return -------------------------------


def test_return_transaction_functional_frees_the_asset(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RET-1", status="borrowed")
    member_id = make_member(db_conn, "MEM-RET-1")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["condition"] == "functional"
    assert body["status"] == "available"

    txn = db_conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert txn["status"] == "returned"
    assert txn["time_in"] is not None
    assert txn["returned_by_staff_id"] == staff_id

    asset = db_conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["status"] == "available"
    assert asset["condition"] == "functional"
    assert asset["deleted_at"] is None


def test_return_transaction_non_functional_sends_asset_to_maintenance(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RET-2", status="borrowed")
    member_id = make_member(db_conn, "MEM-RET-2")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "inoperable", "return_remarks": "motor won't spin"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200
    assert resp.get_json()["status"] == "maintenance"

    asset = db_conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["status"] == "maintenance"
    assert asset["condition"] == "inoperable"
    assert asset["deleted_at"] is None

    txn = db_conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert txn["return_remarks"] == "motor won't spin"


def test_return_transaction_disposed_soft_deletes_the_asset(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RET-3", status="borrowed")
    member_id = make_member(db_conn, "MEM-RET-3")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "disposed"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200

    asset = db_conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
    assert asset["condition"] == "disposed"
    assert asset["deleted_at"] is not None


def test_return_already_returned_transaction_is_rejected(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RET-4", status="borrowed")
    member_id = make_member(db_conn, "MEM-RET-4")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    first = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert first.status_code == 200

    second = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )
    assert second.status_code == 400
    assert "already been returned" in second.get_json()["error"]


def test_return_transaction_rejects_invalid_condition(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RET-5", status="borrowed")
    member_id = make_member(db_conn, "MEM-RET-5")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/return",
        json={"condition_in": "melted"},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400
    assert "condition_in must be one of" in resp.get_json()["error"]

    # rejected up front -- the transaction must still be untouched
    txn = db_conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert txn["status"] == "borrowed"


def test_return_nonexistent_transaction_404s(client, db_conn):
    _, csrf = login_as_admin(client, db_conn)
    resp = client.post(
        "/api/v1/transactions/999999/return",
        json={},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 404


def test_return_transaction_requires_login(client, db_conn):
    resp = client.post("/api/v1/transactions/1/return", json={})
    assert resp.status_code == 401


# # # Table listing: /transactions/table ------------------------------------


def test_table_accepts_sort_by_time_in(client, db_conn):
    """time_in (rendered as the Return Date column) was added to
    TRANSACTIONS_SORTABLE_COLUMNS alongside the table's new column --
    this guards against that mapping silently regressing back to a 400."""
    staff_id, _ = login_as_admin(client, db_conn)
    resp = client.get("/api/v1/transactions/table?sort_by=time_in&sort_dir=asc")
    assert resp.status_code == 200


def test_table_rejects_unknown_sort_column(client, db_conn):
    login_as_admin(client, db_conn)
    resp = client.get("/api/v1/transactions/table?sort_by=not_a_real_column")
    assert resp.status_code == 400
    assert "sort_by must be one of" in resp.get_json()["error"]


def test_table_status_filter_overdue_excludes_non_overdue_loans(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)
    asset_a = make_asset(db_conn, "AST-TBL-1", status="borrowed")
    member_a = make_member(db_conn, "MEM-TBL-1")
    make_transaction(db_conn, asset_a, member_a, staff_id, due_date=PAST)

    asset_b = make_asset(db_conn, "AST-TBL-2", status="borrowed")
    member_b = make_member(db_conn, "MEM-TBL-2")
    make_transaction(db_conn, asset_b, member_b, staff_id, due_date=FUTURE)

    resp = client.get("/api/v1/transactions/table?status=overdue")
    assert resp.status_code == 200
    assert b'data-total-count="1"' in resp.data


def test_table_status_filter_returned(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)
    asset_a = make_asset(db_conn, "AST-TBL-3", status="available")
    member_a = make_member(db_conn, "MEM-TBL-3")
    make_transaction(
        db_conn, asset_a, member_a, staff_id, status="returned", time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    asset_b = make_asset(db_conn, "AST-TBL-4", status="borrowed")
    member_b = make_member(db_conn, "MEM-TBL-4")
    make_transaction(db_conn, asset_b, member_b, staff_id, status="borrowed")

    resp = client.get("/api/v1/transactions/table?status=returned")
    assert resp.status_code == 200
    assert b'data-total-count="1"' in resp.data


def test_table_requires_login(client, db_conn):
    resp = client.get("/api/v1/transactions/table")
    assert resp.status_code == 401


# # # Stat cards: /transactions/stat-cards ----------------------------------


def test_stat_cards_reflect_current_counts(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)

    asset_a = make_asset(db_conn, "AST-STAT-1", status="borrowed")
    member_a = make_member(db_conn, "MEM-STAT-1")
    make_transaction(db_conn, asset_a, member_a, staff_id, due_date=PAST)  # overdue

    asset_b = make_asset(db_conn, "AST-STAT-2", status="borrowed")
    member_b = make_member(db_conn, "MEM-STAT-2")
    make_transaction(db_conn, asset_b, member_b, staff_id, due_date=FUTURE)  # active, not overdue

    asset_c = make_asset(db_conn, "AST-STAT-3", status="available")
    member_c = make_member(db_conn, "MEM-STAT-3")
    make_transaction(
        db_conn, asset_c, member_c, staff_id, status="returned", time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    )

    resp = client.get("/api/v1/transactions/stat-cards")
    assert resp.status_code == 200

    counts = re.findall(r'text-2xl font-bold mt-1">(\d+)</p>', resp.get_data(as_text=True))
    # Rendered in template order: All, Active Loans, Overdue, Returned.
    assert counts == ["3", "2", "1", "1"]


# # # Overdue listing: /transactions/overdue --------------------------------


def test_list_overdue_transactions_only_returns_overdue(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)

    asset_a = make_asset(db_conn, "AST-OVR-1", status="borrowed")
    member_a = make_member(db_conn, "MEM-OVR-1")
    overdue_id = make_transaction(db_conn, asset_a, member_a, staff_id, due_date=PAST)

    asset_b = make_asset(db_conn, "AST-OVR-2", status="borrowed")
    member_b = make_member(db_conn, "MEM-OVR-2")
    make_transaction(db_conn, asset_b, member_b, staff_id, due_date=FUTURE)

    resp = client.get("/api/v1/transactions/overdue")
    assert resp.status_code == 200
    data = resp.get_json()
    assert [row["id"] for row in data] == [overdue_id]


# # # Renew / extend due date: /transactions/<id>/renew ---------------------


def test_renew_extends_due_date(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RNW-1", status="borrowed")
    member_id = make_member(db_conn, "MEM-RNW-1")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id, due_date=PAST)

    new_due = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    resp = client.post(
        f"/api/v1/transactions/{txn_id}/renew",
        json={"due_date": new_due},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200
    assert resp.get_json()["due_date"] == new_due

    txn = db_conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    assert txn["due_date"] == new_due
    assert txn["status"] == "borrowed"  # renewing must not touch status/time_in
    assert txn["time_in"] is None


def test_renew_rejects_past_due_date(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RNW-2", status="borrowed")
    member_id = make_member(db_conn, "MEM-RNW-2")
    txn_id = make_transaction(db_conn, asset_id, member_id, staff_id)

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/renew",
        json={"due_date": PAST},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_renew_rejects_already_returned_transaction(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)
    asset_id = make_asset(db_conn, "AST-RNW-3", status="available")
    member_id = make_member(db_conn, "MEM-RNW-3")
    txn_id = make_transaction(
        db_conn, asset_id, member_id, staff_id, status="returned",
        time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    resp = client.post(
        f"/api/v1/transactions/{txn_id}/renew",
        json={"due_date": FUTURE},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400
    assert "active loan" in resp.get_json()["error"]


def test_renew_requires_login(client, db_conn):
    resp = client.post("/api/v1/transactions/1/renew", json={"due_date": FUTURE})
    assert resp.status_code == 401


# # # Batch return: /transactions/batch-return[-confirm] --------------------


def test_batch_return_confirm_lists_only_open_loans(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)
    asset_a = make_asset(db_conn, "AST-BAT-1", status="borrowed")
    member_a = make_member(db_conn, "MEM-BAT-1")
    open_id = make_transaction(db_conn, asset_a, member_a, staff_id)

    asset_b = make_asset(db_conn, "AST-BAT-2", status="available")
    member_b = make_member(db_conn, "MEM-BAT-2")
    returned_id = make_transaction(
        db_conn, asset_b, member_b, staff_id, status="returned",
        time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    resp = client.get(f"/api/v1/transactions/batch-return-confirm?transaction_ids={open_id}&transaction_ids={returned_id}")
    assert resp.status_code == 200
    assert b"AST-BAT-1" in resp.data
    assert b"AST-BAT-2" not in resp.data


def test_batch_return_confirm_requires_ids(client, db_conn):
    login_as_admin(client, db_conn)
    resp = client.get("/api/v1/transactions/batch-return-confirm")
    assert resp.status_code == 400


def test_batch_return_marks_all_selected_as_returned(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)

    asset_a = make_asset(db_conn, "AST-BAT-3", status="borrowed")
    member_a = make_member(db_conn, "MEM-BAT-3")
    txn_a = make_transaction(db_conn, asset_a, member_a, staff_id)

    asset_b = make_asset(db_conn, "AST-BAT-4", status="borrowed")
    member_b = make_member(db_conn, "MEM-BAT-4")
    txn_b = make_transaction(db_conn, asset_b, member_b, staff_id)

    resp = client.post(
        "/api/v1/transactions/batch-return",
        json={"transaction_ids": [txn_a, txn_b], "condition_in": "functional"},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert sorted(body["returned"]) == sorted([txn_a, txn_b])
    assert body["skipped"] == []

    for txn_id, asset_id in [(txn_a, asset_a), (txn_b, asset_b)]:
        txn = db_conn.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
        assert txn["status"] == "returned"
        asset = db_conn.execute("SELECT * FROM assets WHERE id = ?", (asset_id,)).fetchone()
        assert asset["status"] == "available"


def test_batch_return_skips_already_returned_without_failing_the_rest(client, db_conn):
    staff_id, csrf = login_as_admin(client, db_conn)

    asset_a = make_asset(db_conn, "AST-BAT-5", status="borrowed")
    member_a = make_member(db_conn, "MEM-BAT-5")
    open_txn = make_transaction(db_conn, asset_a, member_a, staff_id)

    asset_b = make_asset(db_conn, "AST-BAT-6", status="available")
    member_b = make_member(db_conn, "MEM-BAT-6")
    already_returned_txn = make_transaction(
        db_conn, asset_b, member_b, staff_id, status="returned",
        time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    resp = client.post(
        "/api/v1/transactions/batch-return",
        json={"transaction_ids": [open_txn, already_returned_txn, 999999]},
        headers={"X-CSRF-Token": csrf},
    )

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["returned"] == [open_txn]
    assert sorted(body["skipped"]) == sorted([already_returned_txn, 999999])


def test_batch_return_rejects_empty_list(client, db_conn):
    _, csrf = login_as_admin(client, db_conn)
    resp = client.post(
        "/api/v1/transactions/batch-return",
        json={"transaction_ids": []},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_batch_return_rejects_oversized_batch(client, db_conn):
    _, csrf = login_as_admin(client, db_conn)
    resp = client.post(
        "/api/v1/transactions/batch-return",
        json={"transaction_ids": list(range(1, 52))},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 400


def test_batch_return_requires_login(client, db_conn):
    resp = client.post("/api/v1/transactions/batch-return", json={"transaction_ids": [1]})
    assert resp.status_code == 401


# # # Export: /transactions/export -------------------------------------


def test_export_returns_csv_with_expected_rows(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)
    asset_a = make_asset(db_conn, "AST-EXP-1", status="borrowed")
    member_a = make_member(db_conn, "MEM-EXP-1")
    make_transaction(db_conn, asset_a, member_a, staff_id)

    asset_b = make_asset(db_conn, "AST-EXP-2", status="available")
    member_b = make_member(db_conn, "MEM-EXP-2")
    make_transaction(
        db_conn, asset_b, member_b, staff_id, status="returned",
        time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    resp = client.get("/api/v1/transactions/export")
    assert resp.status_code == 200
    assert resp.headers["Content-Type"].startswith("text/csv")
    assert "attachment" in resp.headers["Content-Disposition"]

    body = resp.get_data(as_text=True).encode("utf-8").decode("utf-8-sig")
    lines = [line for line in body.splitlines() if line]
    assert len(lines) == 3  # header + 2 rows
    assert "AST-EXP-1" in body
    assert "AST-EXP-2" in body


def test_export_respects_status_filter(client, db_conn):
    staff_id, _ = login_as_admin(client, db_conn)
    asset_a = make_asset(db_conn, "AST-EXP-3", status="borrowed")
    member_a = make_member(db_conn, "MEM-EXP-3")
    make_transaction(db_conn, asset_a, member_a, staff_id)

    asset_b = make_asset(db_conn, "AST-EXP-4", status="available")
    member_b = make_member(db_conn, "MEM-EXP-4")
    make_transaction(
        db_conn, asset_b, member_b, staff_id, status="returned",
        time_in=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    resp = client.get("/api/v1/transactions/export?status=returned")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "AST-EXP-4" in body
    assert "AST-EXP-3" not in body


def test_export_requires_login(client, db_conn):
    resp = client.get("/api/v1/transactions/export")
    assert resp.status_code == 401
