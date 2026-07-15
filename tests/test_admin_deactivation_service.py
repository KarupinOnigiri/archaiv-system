from services.admin_deactivation_service import (
    determine_approval_tier,
    finalize_if_ready,
)
from tests.conftest import make_staff


def test_tier_is_admin_when_third_admin_exists(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    make_staff(db_conn, "admin", "A3")  # the "third" admin
    tier, required = determine_approval_tier(db_conn, requester, target)
    assert (tier, required) == ("admin", 1)


def test_tier_falls_to_in_charge_when_fewer_than_3_admins(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    make_staff(db_conn, "in_charge", "I1")
    make_staff(db_conn, "in_charge", "I2")
    tier, required = determine_approval_tier(db_conn, requester, target)
    assert (tier, required) == ("in_charge", 2)


def test_tier_falls_to_tool_keeper_when_fewer_than_2_in_charge(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    make_staff(db_conn, "in_charge", "I1")  # only 1
    make_staff(db_conn, "tool_keeper", "T1")
    tier, required = determine_approval_tier(db_conn, requester, target)
    assert (tier, required) == ("tool_keeper", 1)


def test_no_tier_available_returns_none(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    tier, required = determine_approval_tier(db_conn, requester, target)
    assert (tier, required) == (None, None)


def test_inactive_and_excluded_staff_dont_count(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    make_staff(db_conn, "admin", "A3", is_active=0)  # inactive — shouldn't count
    tier, required = determine_approval_tier(db_conn, requester, target)
    assert (tier, required) == (None, None)


def test_finalize_deactivates_target_once_threshold_met(db_conn):
    requester = make_staff(db_conn, "admin", "A1")
    target = make_staff(db_conn, "admin", "A2")
    approver = make_staff(db_conn, "in_charge", "I1")
    make_staff(db_conn, "in_charge", "I2")

    cur = db_conn.execute(
        "INSERT INTO admin_deactivation_requests "
        "(target_staff_id, requested_by_staff_id, reason, required_tier, required_approvals) "
        "VALUES (?, ?, 'test', 'in_charge', 2)",
        (target, requester),
    )
    db_conn.commit()
    request_id = cur.lastrowid

    db_conn.execute(
        "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id) VALUES (?, ?)",
        (request_id, approver),
    )
    db_conn.commit()

    request_row = db_conn.execute(
        "SELECT * FROM admin_deactivation_requests WHERE id = ?", (request_id,)
    ).fetchone()

    # only 1 of 2 required approvals so far
    assert finalize_if_ready(request_row, db_conn) is False

    second_approver = make_staff(db_conn, "in_charge", "I3")
    db_conn.execute(
        "INSERT INTO admin_deactivation_approvals (request_id, approver_staff_id) VALUES (?, ?)",
        (request_id, second_approver),
    )
    db_conn.commit()

    assert finalize_if_ready(request_row, db_conn) is True
    target_row = db_conn.execute(
        "SELECT * FROM staff WHERE id = ?", (target,)
    ).fetchone()
    assert target_row["is_active"] == 0
