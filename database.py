import sqlite3
from flask import request, jsonify
import os

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "instance", "schema.sql")
DB_PATH = os.path.join(os.path.dirname(__file__), "instance", "toolroom.db")


def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


# add after get_db_connection()
def init_db():
    """Creates the database schema if it doesn't exist yet.
    Safe to call on every app startup — no-op if schema is already present."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)

    conn = get_db_connection()
    table_exists = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='staff'"
    ).fetchone()

    if table_exists is None:
        with open(SCHEMA_PATH, "r") as f:
            conn.executescript(f.read())
        conn.commit()
        print("Database schema created.")

    conn.close()


def get_json_or_error():
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    return data, None
