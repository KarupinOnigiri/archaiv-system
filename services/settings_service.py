from database import get_db_connection

# Sentinel for "locked with no expiry" in staff.locked_until, without a schema change.
INDEFINITE_LOCK_UNTIL = "9999-12-31 23:59:59"

# Same sentinel idea, for assigned_end -- lets an assignment be "Permanent" without a schema change.
PERMANENT_ASSIGNED_END = "9999-12-31"

# Fallback only -- real numbers live in app_settings; a missing row shouldn't crash a login.
DEFAULT_SETTINGS = {
    "session_idle_minutes": 30,
    "session_absolute_hours": 12,
    "max_failed_attempts": 5,
    "lockout_minutes": 15,
    "due_soon_hours": 24,
    "default_loan_days": 2,
    "damage_restriction_threshold": 2,
    "auto_backup_interval_hours": 24,
    "archive_purge_days": 0,  # '0' means "None" -- auto-purge disabled.
}

# Feature flags default to enabled; a missing row should never silently lock one out.
DEFAULT_FEATURE_FLAGS = {
    "feature_assets_enabled": True,
    "feature_members_enabled": True,
    "feature_transactions_enabled": True,
    "feature_requests_enabled": True,
    "feature_reports_enabled": True,
}

# Short name -> (app_settings key, human label for disabled/redirect messages).
FEATURE_FLAGS = {
    "assets": ("feature_assets_enabled", "Inventory"),
    "members": ("feature_members_enabled", "Members"),
    "transactions": ("feature_transactions_enabled", "Borrow & Return"),
    "requests": ("feature_requests_enabled", "Approvals"),
    "reports": ("feature_reports_enabled", "Reports"),
}


def get_setting_int(key, conn=None):
    """Looks up an int setting by key, falling back to DEFAULT_SETTINGS if missing/unparseable."""
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


def get_default_loan_days(conn=None):
    return get_setting_int("default_loan_days", conn)


def get_damage_restriction_threshold(conn=None):
    return get_setting_int("damage_restriction_threshold", conn)


def get_auto_backup_interval_hours(conn=None):
    return get_setting_int("auto_backup_interval_hours", conn)


def get_archive_purge_days(conn=None):
    """0 means "None" -- auto-purge disabled; otherwise days since deleted_at/deactivated_at."""
    return get_setting_int("archive_purge_days", conn)


def get_setting_bool(key, conn=None):
    """Same fallback pattern as get_setting_int, for '1'/'0' feature-flag rows."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return DEFAULT_FEATURE_FLAGS.get(key, True)
        return row["value"] == "1"
    finally:
        if owns_conn:
            conn.close()


def get_auto_backup_enabled(conn=None):
    """Falls back to True by default, like the feature flags' own safety-first default."""
    return get_setting_bool("auto_backup_enabled", conn)


def get_feature_flags(conn=None):
    """Returns {name: enabled} for every FEATURE_FLAGS entry; used by templates and route gating."""
    owns_conn = conn is None
    if owns_conn:
        conn = get_db_connection()
    try:
        return {
            name: get_setting_bool(key, conn)
            for name, (key, _label) in FEATURE_FLAGS.items()
        }
    finally:
        if owns_conn:
            conn.close()


def is_feature_enabled(name, conn=None):
    """Single-flag convenience wrapper around get_feature_flags for callers checking just one."""
    key, _label = FEATURE_FLAGS[name]
    return get_setting_bool(key, conn)


def get_feature_label(name):
    return FEATURE_FLAGS[name][1]
