"""Test infrastructure: an isolated on-disk SQLite DB per test (real schema,
via the real migrations - not mocked), a Flask test client, and fixtures for
the entities almost every test needs (a department, a member, a logged-in
staff account of each role, an asset).

Isolation strategy: TOOLROOM_INSTANCE_DIR / TOOLROOM_TESTING are read by
database.py / app.py (see the comments there) to point DB_PATH at a fresh
temp file and skip starting the background scheduler. The app module itself
is only ever imported once per test session (importing it re-registers
Flask routes/blueprints, which Flask doesn't allow twice) - each test gets
DB isolation instead by swapping out database.DB_PATH directly and
re-running migrations against a brand new temp file. Every route/service in
this codebase reaches the DB through `database.get_db_connection()`, which
reads `database.DB_PATH` at call time, not at import time - so this swap
affects the whole app immediately, no re-import needed (verified: only
backup_db.py and tools/encrypt_existing_db.py bind DB_PATH by value instead,
and neither is exercised through the Flask app).

`make_member`/`make_staff`/`make_asset` (plain functions, not fixtures) are
importable by other test modules that need more than one of an entity, or
need non-default fields (e.g. a tool_keeper with an assignment window).
"""

import os
import sys
import tempfile
import shutil

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

# Must be set before the first `import database` / `import app` anywhere in
# the process - both read environment variables at module-import time.
_SESSION_TMP = tempfile.mkdtemp(prefix="toolroom_test_")
os.environ["TOOLROOM_INSTANCE_DIR"] = _SESSION_TMP
os.environ["TOOLROOM_TESTING"] = "1"
os.environ.setdefault("SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("FLASK_DEBUG", "0")

import database  # noqa: E402
import app as app_module  # noqa: E402  (imports/starts the Flask app exactly once)
from extensions import limiter  # noqa: E402
from services.login_service import start_session  # noqa: E402
from services.member_service import create_member, add_department  # noqa: E402
from services.staff_service import create_staff  # noqa: E402
from services.asset_service import (  # noqa: E402
    create_asset,
    add_category,
    _ensure_category_sort_order_column,
)

TEST_PASSWORD = "TestPassword123!"


@pytest.fixture(scope="session", autouse=True)
def _cleanup_session_tmp():
    yield
    shutil.rmtree(_SESSION_TMP, ignore_errors=True)


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Points database.DB_PATH at a brand-new file for this test only, runs
    every migration against it, and hands back the path. Every route/service
    call made during this test (through the Flask app or directly) will use
    this database, since they all resolve DB_PATH dynamically."""
    db_path = os.path.join(str(tmp_path), "test.db")
    instance_dir = str(tmp_path)
    monkeypatch.setattr(database, "DB_PATH", db_path)
    monkeypatch.setattr(database, "INSTANCE_DIR", instance_dir)
    database.init_db()
    # asset_service.py adds categories.sort_order lazily, once, at module
    # import time - which already happened against the session-init DB
    # before this test's DB existed, so it never ran against this one.
    # Re-run it now so add_category()/category_id both work per test.
    _ensure_category_sort_order_column()
    # flask-limiter's in-memory storage is process-wide and `app` is only
    # ever created once for the whole test session (see module docstring)
    # - without this, request counts from an earlier test's /login
    # attempts carry over and trip a 429 in a later, unrelated test.
    limiter.reset()
    return db_path


@pytest.fixture
def conn(fresh_db):
    """A direct DB connection for setup/assertions. Closed after the test."""
    c = database.get_db_connection()
    yield c
    c.close()


@pytest.fixture
def client(fresh_db):
    """Flask test client wired to the same fresh_db (both fixtures depend on
    the same monkeypatched database.DB_PATH within one test)."""
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def second_client(fresh_db):
    """A second, independent test client (separate cookie jar) on the same
    fresh_db - for tests that need two different staff members acting at
    once (e.g. one files something, a different one reviews it). `client`
    alone can't do this: logging in a second staff on it just overwrites
    the first one's session cookie in the same jar, it doesn't add a second
    concurrent session."""
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


@pytest.fixture
def department_id(conn):
    result, error = add_department(conn, "Electrical Installation and Maintenance", staff_id=None)
    assert error is None, error
    return result["id"]


def make_member(conn, department_id, member_type="trainee", **overrides):
    data = {
        "first_name": "Test",
        "last_name": "Member",
        "member_type": member_type,
        "department_id": department_id,
    }
    data.update(overrides)
    result, error = create_member(conn, data, staff_id=None)
    assert error is None, error
    return result["id"]


@pytest.fixture
def member_id(conn, department_id):
    return make_member(conn, department_id)


def make_staff(conn, department_id, role, username, **overrides):
    member = make_member(
        conn, department_id, member_type="trainer", first_name=role.title(), last_name="Account"
    )
    data = {
        "member_id": member,
        "username": username,
        "password": TEST_PASSWORD,
        "role": role,
    }
    data.update(overrides)
    result, error = create_staff(conn, data, staff_id=None)
    assert error is None, error
    return result["id"]


@pytest.fixture
def admin_id(conn, department_id):
    return make_staff(conn, department_id, "admin", "test_admin")


@pytest.fixture
def in_charge_id(conn, department_id):
    return make_staff(conn, department_id, "in_charge", "test_in_charge")


@pytest.fixture
def tool_keeper_id(conn, department_id):
    return make_staff(conn, department_id, "tool_keeper", "test_tool_keeper")


def login_as(client, conn, staff_id):
    """Logs `client` in as `staff_id` without going through the HTTP login
    route (that's exercised separately in test_auth.py) - sets the session
    cookie and returns the csrf token every state-changing request needs in
    the X-CSRF-Token header."""
    token, csrf_token = start_session(staff_id)
    client.set_cookie("session_token", token, domain="localhost")
    return csrf_token


@pytest.fixture
def as_admin(client, conn, admin_id):
    csrf = login_as(client, conn, admin_id)
    return client, csrf, admin_id


@pytest.fixture
def as_in_charge(client, conn, in_charge_id):
    csrf = login_as(client, conn, in_charge_id)
    return client, csrf, in_charge_id


@pytest.fixture
def as_tool_keeper(client, conn, tool_keeper_id):
    csrf = login_as(client, conn, tool_keeper_id)
    return client, csrf, tool_keeper_id


@pytest.fixture
def category_id(conn):
    result, error = add_category(conn, "Power Tools", staff_id=None)
    assert error is None, error
    return result["id"]


def make_asset(conn, category_id=None, **overrides):
    data = {"name": "Test Drill", "category_id": category_id}
    data.update(overrides)
    result, error = create_asset(conn, data, None, {}, staff_id=None)
    assert error is None, error
    return result["id"]


@pytest.fixture
def asset_id(conn, category_id):
    return make_asset(conn, category_id)
