import sqlite3
from flask import request, jsonify
import os
import logging

logger = logging.getLogger(__name__)

INSTANCE_DIR = os.path.join(os.path.dirname(__file__), "instance")
DB_PATH = os.path.join(INSTANCE_DIR, "toolroom.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")

# The very first migration file. Installs that already have a database from
# before migrations existed already have this schema applied — see init_db().
BASELINE_MIGRATION = "0001_initial.sql"


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
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
    """Installs that predate the migrations folder already have the full
    schema applied the old way (a single schema.sql run by the old
    init_db()). Detect that case and record the baseline migration as
    already applied, instead of re-running CREATE TABLE statements that
    would fail against existing tables."""
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
            f"Detected pre-existing schema — recorded {BASELINE_MIGRATION} as applied."
        )

    return applied


def _write_schema_snapshot(conn):
    """Regenerates instance/schema.sql from the live database's actual
    schema -- CREATE TABLE/INDEX/TRIGGER/VIEW statements only, never row
    data (unlike sqlite3.Connection.iterdump(), which would also dump
    every table's actual contents -- exactly what a version-controlled
    reference file must NOT contain). No ORDER BY on purpose: sqlite_master
    returns rows in original creation order, which is what made the old
    hand-dumped schema.sql (`sqlite3 toolroom.db .schema > schema.sql`)
    read the way it does -- matching that means this replacement doesn't
    reshuffle the file's whole layout on the next migration.

    This file was never read by the app itself (see BASELINE_MIGRATION's
    docstring -- migrations/*.sql, replayed incrementally, is the actual
    source of truth); it's a checked-in, at-a-glance reference for anyone
    reading the codebase without spinning up the database. Called from
    init_db() only when at least one migration just ran, so an ordinary
    startup with nothing pending doesn't rewrite this file for no reason."""
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
        # A stale schema.sql is a paper-cut, not a reason to fail startup
        # -- the migration itself already committed successfully above.
        logger.warning(f"Could not refresh instance/schema.sql: {e}")


def init_db():
    """Applies any migration files in migrations/ that haven't run yet, in
    filename order. Safe to call on every app startup — no-op if everything
    is already applied. Each migration runs inside its own transaction."""
    if INSTANCE_DIR and not os.path.exists(INSTANCE_DIR):
        os.makedirs(INSTANCE_DIR, exist_ok=True)

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


def get_json_or_error():
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    return data, None
