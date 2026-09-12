from flask import request, jsonify
import os
import logging
import threading

logger = logging.getLogger(__name__)


# TOOLROOM_INSTANCE_DIR lets the test suite (tests/conftest.py) point this at
# an isolated temp directory instead of the real instance/ - unset in every
# real deployment, so production behavior is unchanged.
INSTANCE_DIR = os.environ.get("TOOLROOM_INSTANCE_DIR") or os.path.join(
    os.path.dirname(__file__), "instance"
)
DB_PATH = os.path.join(INSTANCE_DIR, "toolroom.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

# First migration file - pre-migrations installs already have this schema, see init_db().
BASELINE_MIGRATION = "0001_initial.sql"

# Optional at-rest encryption via SQLCipher; doesn't protect against a stolen SD card.
DB_ENCRYPTION_KEY = (os.environ.get("DB_ENCRYPTION_KEY") or "").strip() or None

if DB_ENCRYPTION_KEY:
    # PRAGMA key needs a literal string, so reject any single-quote in the key here.
    if "'" in DB_ENCRYPTION_KEY:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY can't contain a single-quote character - "
            "generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        # Every module imports sqlite3 from here, so their except clauses match whichever driver's active.
        from sqlcipher3 import dbapi2 as sqlite3
    except ImportError as e:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY is set but the sqlcipher3 package isn't "
            "installed (it needs libsqlcipher-dev on the system first - "
            "see SECURITY.md). Install with: "
            "pip install -r requirements-encryption.txt"
        ) from e
else:
    import sqlite3


def _open_new_connection():
    """The real connection-opening logic, unconditionally creating a brand
    new one - what get_db_connection() used to do unconditionally, before
    pooling (below) was added. Still used directly for every call under
    pytest, and as the thing the pool opens once per thread otherwise.

    Returns: sqlite3.Connection nga row_factory=sqlite3.Row."""
    # Python-level wait for the connection lock, separate from PRAGMA busy_timeout below.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if DB_ENCRYPTION_KEY:
        # Must run first - SQLCipher derives the cipher from this before anything else.
        # This one line is the entire reason connection pooling exists below:
        # SQLCipher's key derivation (PBKDF2, deliberately expensive to resist
        # brute-forcing the key) measured ~365ms on this app's Pi 4 - paid
        # again on EVERY new connection. A single page load opens more than
        # one (the base.html context processor's own connection, plus the
        # route's own), so opening a fresh connection per call/request was
        # costing 700ms+ of pure connection setup on every navigation once
        # DB_ENCRYPTION_KEY was turned on - unnoticeable before that, since a
        # plain unencrypted SQLite connection is close to free to open.
        conn.execute(f"PRAGMA key = '{DB_ENCRYPTION_KEY}'")

    # These settings matter more on a Pi's slow SD card than on a dev machine.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers keep working during a writer's commit - fixes most "database is locked" errors.
    conn.execute("PRAGMA journal_mode = WAL")
    # NORMAL only fsyncs at checkpoints, safe because WAL already guarantees crash consistency.
    conn.execute("PRAGMA synchronous = NORMAL")
    # Retries for up to 5s on a write-lock collision instead of failing instantly.
    conn.execute("PRAGMA busy_timeout = 5000")
    # Keeps ORDER BY/GROUP BY spillage in RAM instead of on the SD card.
    conn.execute("PRAGMA temp_store = MEMORY")
    # ~64MB cache per connection, sized for this app's Pi 4B 8GB target.
    conn.execute("PRAGMA cache_size = -65536")
    return conn


# Thread-local, not a global dict - Waitress serves each request on one of a
# fixed pool of worker threads (WAITRESS_THREADS), and a raw sqlite3.Connection
# isn't safe to hand to two threads at once. Since only one request runs per
# thread at a time, each thread owning and reusing exactly one connection is
# both safe and exactly what avoids re-paying the SQLCipher cost above on
# every request. Keyed by DB_PATH (not just "the" connection) so the rare
# case of DB_PATH actually changing at runtime - which the test suite does
# constantly via TOOLROOM_INSTANCE_DIR/monkeypatch, though pooling itself is
# skipped entirely under pytest, see below - can never silently hand back a
# connection open on the wrong file.
_pool = threading.local()


class _PooledConnectionProxy:
    """Wraps a pooled, thread-owned connection so every one of this
    codebase's existing `conn = get_db_connection(); try: ... finally:
    conn.close()` call sites (there are hundreds) keeps working completely
    unmodified - close() here just returns it to the pool instead of
    actually closing the underlying connection, which is the entire point:
    those call sites closing the real connection every time is exactly what
    was forcing a fresh (expensive, see _open_new_connection) connection to
    be opened on every single call. Everything else - execute, commit,
    rollback, row_factory, the `with conn:` transaction protocol - passes
    straight through to the real connection via attribute delegation."""

    def __init__(self, real_conn):
        object.__setattr__(self, "_real_conn", real_conn)

    def close(self):
        pass  # Intentionally not closing - see class docstring.

    def __getattr__(self, name):
        return getattr(self._real_conn, name)

    def __setattr__(self, name, value):
        setattr(self._real_conn, name, value)

    def __enter__(self):
        return self._real_conn.__enter__()

    def __exit__(self, *args):
        return self._real_conn.__exit__(*args)


def get_db_connection():
    """The one function the whole app uses for a DB connection - never
    sqlite3/sqlcipher3 directly.

    Under pytest, always opens a genuinely fresh connection (pooling is
    skipped entirely) - the test suite relies on being able to actually
    close a connection and have the underlying file release cleanly
    (TOOLROOM_INSTANCE_DIR points at a fresh temp dir per test; on Windows
    especially, a file can't even be deleted while a connection to it is
    still technically open, which a pooled "close() is a no-op" connection
    would silently violate).

    Outside pytest (the real app under Waitress), returns this thread's one
    pooled connection - opening it fresh only the first time this thread
    ever needs one (or if DB_PATH has changed since, which doesn't happen
    in production but keeps the pool correct regardless). See
    _PooledConnectionProxy for why every existing call site's own
    conn.close() doesn't defeat this.

    Returns: sqlite3.Connection (fresh, under pytest) or
    _PooledConnectionProxy (reused, otherwise) - either way, callers keep
    calling .close() when they're done exactly as before."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return _open_new_connection()

    cached = getattr(_pool, "conn", None)
    cached_path = getattr(_pool, "path", None)
    if cached is not None and cached_path == DB_PATH:
        try:
            cached.execute("SELECT 1")
            return _PooledConnectionProxy(cached)
        except sqlite3.Error:
            # The real connection died underneath us (e.g. the process it
            # belonged to was somehow torn down) - fall through and open a
            # replacement rather than handing back something broken.
            pass

    fresh = _open_new_connection()
    _pool.conn = fresh
    _pool.path = DB_PATH
    return _PooledConnectionProxy(fresh)


def _ensure_migrations_table(conn):
    """Mo-himo sa schema_migrations table kung wala pa - gina-check ni sa
    init_db() aron dili na ma-rerun ang migration nga na-apply na."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
        )
        """)
    conn.commit()


def _applied_migrations(conn):
    """Mobalik sa set sa migration filenames nga na-apply na ani nga db."""
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def _bootstrap_pre_migration_db(conn, applied):
    """Ang daan nga install naa nay kompleto nga schema bisan wala pa
    ni-agi sa migration system - i-mark ra ang baseline migration nga
    "applied" instead of re-running CREATE TABLE nga sigurado mag-error."""
    if BASELINE_MIGRATION in applied:
        return applied

    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='staff'"
    ).fetchone()

    if table_exists is not None:
        conn.execute(
            "INSERT INTO schema_migrations (filename) VALUES (?)",
            (BASELINE_MIGRATION,),
        )
        conn.commit()
        applied.add(BASELINE_MIGRATION)
        logger.info(
            f"Detected pre-existing schema - recorded {BASELINE_MIGRATION} as applied."
        )

    return applied


def _write_schema_snapshot(conn):
    """Mo-regenerate sa instance/schema.sql gikan sa live schema karon -
    para ra ni sa mga tawo nga mubasa; migrations/*.sql ang source of
    truth gihapon."""
    rows = conn.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
    ).fetchall()
    schema_path = os.path.join(INSTANCE_DIR, "schema.sql")
    try:
        with open(schema_path, "w", encoding="utf-8") as f:
            for row in rows:
                f.write(row["sql"].rstrip())
                f.write(";\n")
        logger.info("Refreshed instance/schema.sql from the live schema.")
    except OSError as e:
        # Not fatal - the migration already committed; a stale schema.sql is just a paper-cut.
        logger.warning(f"Could not refresh instance/schema.sql: {e}")


def _chmod_best_effort(path, mode):
    """POSIX-only, best-effort - mag-try ra mo-change og file permission,
    mapakyas man i-log ra, dili maka-crash sa app."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning(f"Could not set permissions on {path}: {e}")


def harden_data_permissions():
    """I-lock ang instance/ ug .env sa tag-iya ra nga user - gitawag matag
    startup sa app."""
    _chmod_best_effort(INSTANCE_DIR, 0o700)
    for name in ("toolroom.db", "toolroom.db-wal", "toolroom.db-shm"):
        path = os.path.join(INSTANCE_DIR, name)
        if os.path.exists(path):
            _chmod_best_effort(path, 0o600)
    if os.path.exists(ENV_PATH):
        _chmod_best_effort(ENV_PATH, 0o600)


def harden_backup_file(path):
    """Same lockdown sa live database, apply lang sa usa ka backup file."""
    _chmod_best_effort(path, 0o600)


def init_db():
    """Mag-apply sa mga wala pa na-run nga migration sa migrations/,
    sunod sa filename order. Safe matag startup - walay mahitabo kung
    na-apply na tanan."""
    if INSTANCE_DIR and not os.path.exists(INSTANCE_DIR):
        os.makedirs(INSTANCE_DIR, exist_ok=True)
    harden_data_permissions()

    conn = get_db_connection()
    _ensure_migrations_table(conn)
    applied = _applied_migrations(conn)
    applied = _bootstrap_pre_migration_db(conn, applied)

    migration_files = sorted(
        f for f in os.listdir(MIGRATIONS_DIR) if f.endswith(".sql")
    )
    pending = [f for f in migration_files if f not in applied]

    for filename in pending:
        path = os.path.join(MIGRATIONS_DIR, filename)
        with open(path, "r") as f:
            script = f.read()
        try:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES (?)", (filename,)
            )
            conn.commit()
            logger.info(f"Applied migration: {filename}")
        except Exception as e:
            conn.rollback()
            conn.close()
            raise RuntimeError(f"Migration {filename} failed: {e}") from e

    if pending:
        _write_schema_snapshot(conn)

    conn.close()
    # Re-run now that toolroom.db actually exists on disk.
    harden_data_permissions()


def get_json_or_error():
    """Mag-parse sa JSON body sa request, dili mag-crash kung walay laman
    o dili JSON - gigamit sa halos tanang POST route.

    Returns: tuple (data, error) - usa ra sa duha ang naay laman."""
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    return data, None
