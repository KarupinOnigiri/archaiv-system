# tests/conftest.py
import os
import tempfile
import pytest

TEST_INSTANCE_DIR = tempfile.mkdtemp(prefix="toolroom_test_")

os.environ.setdefault("SECRET_KEY", "test-secret-key")
os.environ.setdefault("ADMIN_USERNAME", "test_admin")
os.environ.setdefault("ADMIN_PASSWORD", "test_password_123")
os.environ.setdefault("ADMIN_FIRST_NAME", "Test")
os.environ.setdefault("ADMIN_LAST_NAME", "Admin")
os.environ.setdefault("ADMIN_BARCODE_ID", "TESTADM001")
os.environ.setdefault("FLASK_DEBUG", "0")

import database

database.INSTANCE_DIR = TEST_INSTANCE_DIR
database.DB_PATH = os.path.join(TEST_INSTANCE_DIR, "toolroom_test.db")

import app as app_module  # import AFTER patching DB_PATH — triggers init_db() + seeding against the test DB

TABLES_IN_FK_ORDER = [
    "admin_deactivation_approvals",
    "admin_deactivation_requests",
    "unrestriction_requests",
    "audit_log",
    "sessions",
    "transactions",
    "checkout_session_items",
    "checkout_sessions",
    "staff",
    "members",
    "assets",
]


@pytest.fixture(autouse=True)
def _reset_db():
    """Wipes all rows (schema stays) before every test so tests don't leak state into each other."""
    conn = database.get_db_connection()
    for t in TABLES_IN_FK_ORDER:
        conn.execute(f"DELETE FROM {t}")
    conn.execute("DELETE FROM sqlite_sequence")
    conn.commit()
    conn.close()
    yield


@pytest.fixture()
def db_conn():
    conn = database.get_db_connection()
    yield conn
    conn.close()


@pytest.fixture()
def client():
    """Only tests that request this fixture need a working login, so the
    initial admin is reseeded here rather than in the global _reset_db
    fixture — _reset_db wipes staff for EVERY test (including ones that
    build their own precise admin/in_charge/tool_keeper counts, like
    test_admin_deactivation_service.py), and reseeding there would leave
    an extra always-present admin skewing those counts."""
    from services.seed_service import seed_initial_admin

    seed_initial_admin()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def make_member(conn, barcode, member_type="trainer", department="Dept"):
    dept_row = conn.execute(
        "SELECT id FROM departments WHERE name = ?", (department,)
    ).fetchone()
    if dept_row is None:
        dept_id = conn.execute(
            "INSERT INTO departments (name) VALUES (?)", (department,)
        ).lastrowid
    else:
        dept_id = dept_row["id"]

    cur = conn.execute(
        "INSERT INTO members (first_name, last_name, barcode_id, department_id, member_type) "
        "VALUES ('Test', 'User', ?, ?, ?)",
        (barcode, dept_id, member_type),
    )
    conn.commit()
    return cur.lastrowid


def make_staff(conn, role, barcode, is_active=1):
    member_id = make_member(conn, barcode)
    cur = conn.execute(
        "INSERT INTO staff (member_id, username, password_hash, role, is_active) "
        "VALUES (?, ?, 'x', ?, ?)",
        (member_id, f"user_{barcode}", role, is_active),
    )
    conn.commit()
    return cur.lastrowid
