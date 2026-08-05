# tests/conftest.py
import os
import tempfile
import pytest
from werkzeug.security import generate_password_hash

TEST_INSTANCE_DIR = tempfile.mkdtemp(prefix="toolroom_test_")

os.environ.setdefault("SECRET_KEY", "test-secret-key")
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
    from extensions import limiter

    seed_initial_admin()
    app_module.app.config["TESTING"] = True
    # Real rate limits (e.g. 10/minute on login) are appropriate in
    # production but get tripped by a test suite that logs in once per
    # test in rapid succession — that's the limiter working as designed
    # against a single shared in-memory counter, not a bug under test.
    # flask-limiter reads RATELIMIT_ENABLED into `limiter.enabled` once,
    # at init_app() time (during `import app` above) — setting the config
    # key this late wouldn't be seen, so the flag is flipped directly.
    limiter.enabled = False
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


DEFAULT_TEST_PASSWORD = "test-password-123!"


def make_staff_with_password(conn, role, barcode, password=DEFAULT_TEST_PASSWORD, is_active=1):
    """Like make_staff, but with a real werkzeug password hash instead of
    the literal 'x' -- make_staff's account can never actually log in via
    POST /api/v1/login, since that route checks the hash for real. Use
    this (with login(), below) for any test that needs an authenticated
    client session. is_system_seeded=0 so it counts as a real admin for
    seed_initial_admin()'s "does an active admin already exist" check --
    that function only ever creates the temp setup-admin with a random,
    unknowable password; it never seeds anything matching fixed
    credentials, so a test can't just log in as "the admin" without first
    creating one itself. Returns (staff_id, username)."""
    member_id = make_member(conn, barcode)
    username = f"user_{barcode}"
    cur = conn.execute(
        "INSERT INTO staff (member_id, username, password_hash, role, is_active, is_system_seeded) "
        "VALUES (?, ?, ?, ?, ?, 0)",
        (member_id, username, generate_password_hash(password), role, is_active),
    )
    conn.commit()
    return cur.lastrowid, username


def make_asset(conn, barcode, name="Drill", status="available", condition="functional"):
    cur = conn.execute(
        "INSERT INTO assets (asset_id, name, barcode_id, status, condition) "
        "VALUES (?, ?, ?, ?, ?)",
        (barcode, name, barcode, status, condition),
    )
    conn.commit()
    return cur.lastrowid


def login(client, username, password):
    """Logs in via the test client (which persists the session cookie for
    subsequent requests within the same `with client:` block) and returns
    the CSRF token every state-changing request needs in X-CSRF-Token."""
    resp = client.post("/api/v1/login", json={"username": username, "password": password})
    return resp.get_json()["csrf_token"]
