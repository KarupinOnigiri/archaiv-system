from flask import request, jsonify
import os
import logging

logger = logging.getLogger(__name__)


# TOOLROOM_INSTANCE_DIR lets the test suite (tests/conftest.py) point this at
# an isolated temp directory instead of the real instance/ - unset in every
# real deployment, so production behavior is unchanged.
# (Bisaya: Kung naka-set ang TOOLROOM_INSTANCE_DIR - gigamit ni sa testing
# para dili matandog ang tinuod nga database - kay lahi ra jud nga folder
# ang gamiton, dili ang instance/ nga gigamit sa tinuod nga app.)
INSTANCE_DIR = os.environ.get("TOOLROOM_INSTANCE_DIR") or os.path.join(
    os.path.dirname(__file__), "instance"
)
DB_PATH = os.path.join(INSTANCE_DIR, "toolroom.db")
MIGRATIONS_DIR = os.path.join(os.path.dirname(__file__), "migrations")
ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")

# First migration file - pre-migrations installs already have this schema, see init_db().
BASELINE_MIGRATION = "0001_initial.sql"

# Optional at-rest encryption via SQLCipher; doesn't protect against a stolen SD card.
# (Bisaya: Pwede i-encrypt ang database gamit ang SQLCipher, pero kini dili
# proteksyon kung ang mismong SD card mismo maka-widra/nawala - kay pwede ra
# pod ma-decrypt kung naa ang key.)
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


def get_db_connection():
    """Opens a fresh SQLite connection with every Pi-tuned PRAGMA already
    applied (foreign keys, WAL journaling, busy timeout, etc.) - every
    route/service in this app calls this instead of touching sqlite3
    directly, so the tuning only has to live in one place.
    (Bisaya: Mag-abre ni ug bag-ong koneksyon sa database, naka-set na dayon
    ang mga setting nga gikinahanglan para paspas ug stable bisan sa
    Raspberry Pi - dinhi ra gikan ang tanang koneksyon sa tibuok app, para
    usa ra ka lugar ang bubutangan sa mga setting.)

    Returns:
        sqlite3.Connection (or sqlcipher3's equivalent, when at-rest
        encryption is on) - row_factory is set to sqlite3.Row, so each row
        behaves like a dict (row["name"]) as well as a tuple (row[0]). The
        caller owns this connection and must close() it when done; nothing
        here pools or reuses connections.
        (Bisaya: Mobalik ug koneksyon diin ang matag row pwede tratahon
        pareha sa dictionary - row["name"] - o pwede pod pareha sa tuple.
        Ikaw na bahala mo-close niini human magamit; walay connection
        pooling dinhi, matag tawag bag-o gyud.)
    """
    # Python-level wait for the connection lock, separate from PRAGMA busy_timeout below.
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row

    if DB_ENCRYPTION_KEY:
        # Must run first - SQLCipher derives the cipher from this before anything else.
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


def _ensure_migrations_table(conn):
    """Creates the schema_migrations bookkeeping table if it doesn't exist yet
    - this is the table init_db() checks to know which migration files have
    already run, so a restart never re-applies (and errors on) the same one.
    (Bisaya: Mo-himo sa schema_migrations table kung wala pa ni - kini ang
    gina-check sa init_db() para mahibaw-an kung unsa nga mga migration file
    ang na-apply na, aron dili na sila ma-apply pag-usab pag-restart sa app.)

    Args:
        conn: an open sqlite3.Connection (from get_db_connection()).
        (Bisaya: usa ka bukas nga koneksyon sa database.)
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename TEXT PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT (datetime('now', 'localtime'))
        )
        """)
    conn.commit()


def _applied_migrations(conn):
    """Reads schema_migrations and returns which migration filenames have
    already been applied to this database.
    (Bisaya: Mobasa sa schema_migrations table ug mobalik sa listahan sa mga
    migration file nga na-apply na sa niining database.)

    Args:
        conn: an open sqlite3.Connection.
        (Bisaya: usa ka bukas nga koneksyon sa database.)

    Returns:
        set[str] - filenames like "0001_initial.sql", exactly as they'd
        appear in migrations/. Empty set on a brand-new database.
        (Bisaya: usa ka set sa mga filename - pananglitan "0001_initial.sql"
        - sama sa naa sa migrations/ folder. Walay sulod nga set kung
        bag-o pa gyud nga database.)
    """
    rows = conn.execute("SELECT filename FROM schema_migrations").fetchall()
    return {row["filename"] for row in rows}


def _bootstrap_pre_migration_db(conn, applied):
    """Old installs already have the full schema (applied pre-migrations).
    Detect that and mark the baseline migration as applied instead of
    re-running CREATE TABLE statements that'd fail on existing tables.
    (Bisaya: Ang mga daan nga install naa nay kompleto nga schema bisan wala
    pa ni sila ni-agi sa migration system - kini nga function mo-detect ana
    ug mag-mark sa unang/baseline migration nga "applied" na, imbes nga
    i-rerun ang CREATE TABLE nga sigurado mag-error kay naa na man ang mga
    table.)

    Args:
        conn: an open sqlite3.Connection.
        (Bisaya: usa ka bukas nga koneksyon sa database.)
        applied: the set of migration filenames already known to be applied
            (from _applied_migrations()) - mutated/returned, not just read.
        (Bisaya: ang set sa mga migration filename nga na-apply na - kini
        gi-usab ug gi-return, dili lang gibasa.)

    Returns:
        set[str] - the same `applied` set, with BASELINE_MIGRATION added to
        it when this database turned out to be a pre-migrations install.
        (Bisaya: pareho ra gihapon nga `applied` set, pero naay dugang nga
        BASELINE_MIGRATION kung kini nga database daan pala nga install nga
        wala pa ni-agi sa migration system.)
    """
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
    never row data). No ORDER BY on purpose - sqlite_master's natural creation order
    matches the old hand-dumped file so this doesn't reshuffle it on every migration.
    Just a checked-in reference for humans; migrations/*.sql is the real source of truth.
    (Bisaya: Mo-himo pag-usab sa instance/schema.sql base sa buhi/live nga
    schema karon - CREATE TABLE, CREATE INDEX, ug uban pa, walay laman sa
    mga row/data. Para ra ni sa mga tawo nga mubasa/reference; ang
    migrations/*.sql gihapon ang tinuod nga "source of truth".)

    Args:
        conn: an open sqlite3.Connection, called right after a migration commits.
        (Bisaya: usa ka bukas nga koneksyon, gitawag human ma-commit ang
        migration.)
    """
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
    """POSIX-only, best-effort - a failed chmod is logged, not fatal to startup.
    (Bisaya: Mo-try lang mo-change sa file permissions - POSIX/Linux ra ni
    ge-effect, e.g. sa Raspberry Pi. Sa Windows wala ni'y buhaton. Kung
    mapakyas ang pag-usab, i-log ra, dili ni maka-hawa/crash sa app.)

    Args:
        path: filesystem path whose permissions should change.
        (Bisaya: ang path/lokasyon sa file nga usbon ang permission.)
        mode: the octal permission bits to set, e.g. 0o600 (owner read/write only).
        (Bisaya: ang octal nga permission nga i-set, e.g. 0o600 nga ang
        tag-iya ra ang pwede mobasa ug mosuwat.)
    """
    if os.name != "posix":
        return
    try:
        os.chmod(path, mode)
    except OSError as e:
        logger.warning(f"Could not set permissions on {path}: {e}")


def harden_data_permissions():
    """Locks instance/ and .env to the owning user only; called on every startup.
    (Bisaya: Ge-lock ang instance/ folder ug .env file para ang tag-iya ra
    nga user ang pwede mubasa/mosuwat - gitawag ni sa matag pag-start sa
    app, para bisan asa gipadagan ang app, protektado gihapon kining mga
    sensitive nga file.)
    """
    _chmod_best_effort(INSTANCE_DIR, 0o700)
    for name in ("toolroom.db", "toolroom.db-wal", "toolroom.db-shm"):
        path = os.path.join(INSTANCE_DIR, name)
        if os.path.exists(path):
            _chmod_best_effort(path, 0o600)
    if os.path.exists(ENV_PATH):
        _chmod_best_effort(ENV_PATH, 0o600)


def harden_backup_file(path):
    """Same owner-only lockdown as the live DB, applied to one backup file.
    (Bisaya: Pareha ra sa pag-lock sa live/tinuod nga database, apply lang
    ni sa usa ka backup file - gitawag human mahimo ang usa ka backup.)

    Args:
        path: filesystem path to the backup file that was just created.
        (Bisaya: ang path/lokasyon sa backup file nga bag-o lang gihimo.)
    """
    _chmod_best_effort(path, 0o600)


def init_db():
    """Applies any migration files in migrations/ that haven't run yet, in
    filename order. Safe to call on every app startup - no-op if everything
    is already applied. Each migration runs inside its own transaction.
    (Bisaya: Mag-apply sa mga migration file sa migrations/ folder nga wala
    pa ma-run, sunod sa han-ay/order sa filename. Safe ni tawgon sa matag
    pagsugod sa app - walay mahitabo kung na-apply na tanan. Matag migration
    naa sa kaugalingon niyang transaction, para kung mag-error ang usa, dili
    maapil ang uban.)
    """
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
    """Parses the current Flask request's JSON body, tolerating a
    non-JSON/empty body instead of letting Flask raise a 400 on its own -
    used by every POST route in this app that expects a JSON payload, so the
    error response is consistent app-wide.
    (Bisaya: Mag-basa sa JSON nga gipadala sa request, pero dili ni mag-crash
    kung walay laman o dili JSON ang gipadala - gigamit ni sa halos tanang
    POST route diri sa app nga nag-expect ug JSON, aron pareho ra ang
    porma sa error message bisan asa nga route.)

    Returns:
        tuple (data, error) - exactly one of the two is not None:
        - data: the parsed JSON body as a dict, when parsing succeeded.
        - error: a ready-to-return (jsonify(...), 400) Flask response tuple,
          when the body was missing or not valid JSON.
        (Bisaya: mobalik ug tuple (data, error) - usa lang sa duha ang naay
        sulod: ang `data` naay laman kung na-parse nga maayo ang JSON; ang
        `error` naay laman - andam na i-return nga (jsonify(...), 400) -
        kung walay laman o dili balido nga JSON ang gipadala.)
    """
    data = request.get_json(silent=True)
    if data is None:
        return None, (jsonify({"error": "Request body must be valid JSON"}), 400)
    return data, None
