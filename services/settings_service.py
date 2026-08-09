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

# Same sentinel idea as INDEFINITE_LOCK_UNTIL just above, but for
# staff.assigned_end (a DATE column, not a full datetime) -- lets a
# tool_keeper's assignment be "Permanent" without a schema change or a
# separate boolean/nullable-means-forever column. login()'s tool_keeper
# check (routes/auth.py) still requires assigned_start/assigned_end to be
# set at all (NULL still means "no assignment period, can't log in"), so
# this is what the "Permanent" quick-option in the Add/Edit/Reassign
# staff modals writes instead of leaving assigned_end blank -- it reads
# as far enough in the future to never actually expire, and
# compute_usage_status (routes/staff.py) special-cases this exact string
# the same way the login lockout check special-cases INDEFINITE_LOCK_UNTIL.
PERMANENT_ASSIGNED_END = "9999-12-31"

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
    "default_loan_days": 2,
    "damage_restriction_threshold": 2,
    "auto_backup_interval_hours": 24,
}

# Feature-flag keys (see migrations/0033_feature_flags.sql) all default to
# enabled -- a missing/unparseable row should never silently lock a feature
# out, the same "fail open to the old behavior" rule DEFAULT_SETTINGS above
# already follows for the numeric settings.
DEFAULT_FEATURE_FLAGS = {
    "feature_assets_enabled": True,
    "feature_members_enabled": True,
    "feature_transactions_enabled": True,
    "feature_requests_enabled": True,
    "feature_reports_enabled": True,
}

# Short name (used by feature_required(), templates' feature_flags.*, and
# the System page form) -> (app_settings key, human label used in the
# "<label> is currently disabled" messages and the ?feature_disabled=
# redirect toast).
FEATURE_FLAGS = {
    "assets": ("feature_assets_enabled", "Inventory"),
    "members": ("feature_members_enabled", "Members"),
    "transactions": ("feature_transactions_enabled", "Borrow & Return"),
    "requests": ("feature_requests_enabled", "Approvals"),
    "reports": ("feature_reports_enabled", "Reports"),
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


def get_default_loan_days(conn=None):
    return get_setting_int("default_loan_days", conn)


def get_damage_restriction_threshold(conn=None):
    return get_setting_int("damage_restriction_threshold", conn)


def get_auto_backup_interval_hours(conn=None):
    return get_setting_int("auto_backup_interval_hours", conn)


def get_setting_bool(key, conn=None):
    """Same fallback-on-missing/unparseable pattern as get_setting_int, for
    the '1'/'0' feature-flag rows. Stored as text like every other
    app_settings value -- '1' is enabled, anything else is disabled."""
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
    """Reuses get_setting_bool's fallback-to-True behavior (its missing-row
    fallback is `DEFAULT_FEATURE_FLAGS.get(key, True)`, which resolves to
    plain `True` for any key -- like this one -- that isn't an actual
    feature flag) -- same "never silently turn a safety net off" default
    as the feature flags themselves."""
    return get_setting_bool("auto_backup_enabled", conn)


def get_feature_flags(conn=None):
    """Returns {short_name: enabled_bool} for every entry in FEATURE_FLAGS
    -- fed to the base.html context processor (app.py) so every template
    can gate nav links/dashboard widgets on it, and to feature_required()
    (services/auth_service.py) for route-level enforcement."""
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
    """Single-flag convenience wrapper around get_feature_flags for callers
    (feature_required, sync_loan_notifications) that only care about one
    feature and shouldn't have to build the whole dict."""
    key, _label = FEATURE_FLAGS[name]
    return get_setting_bool(key, conn)


def get_feature_label(name):
    return FEATURE_FLAGS[name][1]
