from datetime import datetime, timedelta
from services.loan_service import (
    check_and_restrict_overdue_member,
    check_and_lift_restriction_if_resolved,
)
from tests.conftest import make_member


def make_asset(conn, barcode):
    cur = conn.execute(
        "INSERT INTO assets (asset_id, name, barcode_id) VALUES ('', 'Drill', ?)",
        (barcode,),
    )
    conn.commit()
    return cur.lastrowid


def test_auto_restrict_when_overdue_transaction_exists(db_conn):
    member_id = make_member(db_conn, "M1")
    asset_id = make_asset(db_conn, "AST1")
    past_due = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "INSERT INTO transactions (asset_id, member_id, due_date, status) VALUES (?, ?, ?, 'borrowed')",
        (asset_id, member_id, past_due),
    )
    db_conn.commit()

    assert check_and_restrict_overdue_member(member_id, db_conn) is True
    member = db_conn.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    assert member["is_restricted"] == 1
    assert member["restriction_source"] == "auto"


def test_no_restriction_when_not_overdue(db_conn):
    member_id = make_member(db_conn, "M2")
    asset_id = make_asset(db_conn, "AST2")
    future_due = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "INSERT INTO transactions (asset_id, member_id, due_date, status) VALUES (?, ?, ?, 'borrowed')",
        (asset_id, member_id, future_due),
    )
    db_conn.commit()

    assert check_and_restrict_overdue_member(member_id, db_conn) is False


def test_auto_lift_when_overdue_item_returned(db_conn):
    member_id = make_member(db_conn, "M3")
    db_conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_source = 'auto' WHERE id = ?",
        (member_id,),
    )
    db_conn.commit()

    assert check_and_lift_restriction_if_resolved(member_id, db_conn) is True
    member = db_conn.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    assert member["is_restricted"] == 0


def test_manual_restriction_is_never_auto_lifted(db_conn):
    member_id = make_member(db_conn, "M4")
    db_conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_source = 'manual' WHERE id = ?",
        (member_id,),
    )
    db_conn.commit()

    assert check_and_lift_restriction_if_resolved(member_id, db_conn) is False
    member = db_conn.execute(
        "SELECT * FROM members WHERE id = ?", (member_id,)
    ).fetchone()
    assert member["is_restricted"] == 1  # unchanged


def test_auto_lift_blocked_if_another_overdue_item_remains(db_conn):
    member_id = make_member(db_conn, "M5")
    asset_id = make_asset(db_conn, "AST5")
    past_due = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
    db_conn.execute(
        "INSERT INTO transactions (asset_id, member_id, due_date, status) VALUES (?, ?, ?, 'borrowed')",
        (asset_id, member_id, past_due),
    )
    db_conn.execute(
        "UPDATE members SET is_restricted = 1, restriction_source = 'auto' WHERE id = ?",
        (member_id,),
    )
    db_conn.commit()

    assert check_and_lift_restriction_if_resolved(member_id, db_conn) is False
