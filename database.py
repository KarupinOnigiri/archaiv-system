from flask import request, jsonify
import os
import logging

logger = logging.getLogger(__name__)


# TOOLROOM_INSTANCE_DIR lets the test suite (tests/conftest.py) point this at
# an isolated temp directory instead of the real instance/ -- unset in every
# real deployment, so production behavior is unchanged.
INSTANCE_DIR = os.environ.get("TOOLROOM_INSTANCE_DIR") or os.path.join(
    os.path.dirname(__file__), "instance"
)
DB_PATH = os.path.join(INSTANCE_DIR, "toolroom.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

# First migration file -- pre-migrations installs already have this schema, see init_db().
BASELINE_MIGRATION = "0001_initial.sql"

# Optional at-rest encryption via SQLCipher; doesn't protect against a stolen SD card.
DB_ENCRYPTION_KEY = (os.environ.get("DB_ENCRYPTION_KEY") or "").strip() or None

if DB_ENCRYPTION_KEY:
    # PRAGMA key needs a literal string, so reject any single-quote in the key here.
    if "'" in DB_ENCRYPTION_KEY:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY can't contain a single-quote character -- "
            "generate one with: python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    try:
        # Every module imports sqlite3 from here, so their except clauses match whichever driver's active.
        from sqlcipher3 import dbapi2 as sqlite3
    except ImportError as e:
        raise RuntimeError(
            "DB_ENCRYPTION_KEY is set but the sqlcipher3 package isn't "
            "installed (it needs libsqlcipher-dev on the system first -- "
            "see SECURITY.md). Install with: "
            "pip install -r requirements-encryption.txt"
        ) from e
else:
    import sqlite3


def get_db_connection():
    # Python-level wait for the connection lock, separate from PRAGMA busy_timeout below.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if DB_ENCRYPTION_KEY:
        # Must run first -- SQLCipher derives the cipher from this before anything else.
        conn.execute(f"PRAGMA key = '{DB_ENCRYPTION_KEY}'")

    # These settings matter more on a Pi's slow SD card than on a dev machine.
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers keep working during a writer's commit -- fixes most "database is locked" errors.
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


def _ensure_migrations_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
        )
        """)
    conn.commit()


def _applied_migrations(conn):
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def _bootstrap_pre_migration_db(conn, applied):
    """Old installs already have the full schema (applied pre-migrations).
    Detect that and mark the baseline migration as applied instead of
    re-running CREATE TABLE statements that'd fail on existing tables."""
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
    """Regenerates instance/schema.sql from the live schema (CREATE TABLE/INDEX/etc,
    never row data). No ORDER BY on purpose -- sqlite_master's natural creation order
    matches the old hand-dumped file so this doesn't reshuffle it on every migration.
    Just a checked-in reference for humans; migrations/*.sql is the real source of truth."""
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
        # Not fatal -- the migration already committed; a stale schema.sql is just a paper-cut.
        logger.warning(f"Could not refresh instance/schema.sql: {e}")


def _chmod_best_effort(path, mode):
    """POSIX-only, best-effort -- a failed chmod is logged, not fatal to startup."""
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning(f"Could not set permissions on {path}: {e}")


def harden_data_permissions():
    """Locks instance/ and .env to the owning user only; called on every startup."""
    _chmod_best_effort(INSTANCE_DIR, 0o700)
    for name in ("toolroom.db", "toolroom.db-wal", "toolroom.db-shm"):
        path = os.path.join(INSTANCE_DIR, name)
        if os.path.exists(path):
            _chmod_best_effort(path, 0o600)
    if os.path.exists(ENV_PATH):
        _chmod_best_effort(ENV_PATH, 0o600)


def harden_backup_file(path):
    """Same owner-only lockdown as the live DB, applied to one backup file."""
    _chmod_best_effort(path, 0o600)


def init_db():
    """Applies any migration files in migrations/ that haven't run yet, in
    filename order. Safe to call on every app startup - no-op if everything
    is already applied. Each migration runs inside its own transaction."""
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
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    return data, None
