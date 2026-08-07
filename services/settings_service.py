from database import get_db_connection

# Stored in staff.locked_until (a plain "%Y-%m-%d %H:%M:%S" string, same
# column the failed-attempts auto-lockout uses) to represent "locked with
# no expiry" without a schema change or a separate boolean column. Chosen
# as the max value strptime/strftime with this format can round-trip
# rather than datetime.max (which has a microsecond component this format
# string drops, so it wouldn't compare equal after a round-trip through
# the database). Both the login lockout check (routes/auth.py) and the
# admin manual-lock UI (routes/staff.py, staff/_table.html) special-case
# this exact string rather than showing a nonsense "try again in ~4
# million minutes" message.
INDEFINITE_LOCK_UNTIL = "9999-12-31 23:59:59"

# Fallback values only -- the real, admin-editable numbers live in the
# app_settings table (see migrations/0018_app_settings.sql and
# routes/staff.py's manage-session-settings endpoints). Used if a key is
# somehow missing from that table (shouldn't happen after the migration
# seeds it, but a missing row should never hard-crash a login or session
# check).
DEFAULT_SETTINGS = {
    "session_idle_minutes": 30,
    "session_absolute_hours": 12,
    "max_failed_attempts": 5,
    "lockout_minutes": 15,
    "due_soon_hours": 24,
}


def get_setting_int(key, conn=None):
    """Looks up an integer app_settings value by key, falling back to
    DEFAULT_SETTINGS[key] if the row is missing or unparseable. Accepts
    an existing connection (so callers that already have one open, like
    require_session, don't need a second one) or opens/closes its own if
    none is passed."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return DEFAULT_SETTINGS[key]
        try:
            return int(row["value"])
        except (TypeError, ValueError):
            return DEFAULT_SETTINGS[key]
    finally:
        if owns_conn:
            conn.close()


def get_session_idle_minutes(conn=None):
    return get_setting_int("session_idle_minutes", conn)


def get_session_absolute_hours(conn=None):
    return get_setting_int("session_absolute_hours", conn)


def get_max_failed_attempts(conn=None):
    return get_setting_int("max_failed_attempts", conn)


def get_lockout_minutes(conn=None):
    return get_setting_int("lockout_minutes", conn)


def get_due_soon_hours(conn=None):
    return get_setting_int("due_soon_hours", conn)
