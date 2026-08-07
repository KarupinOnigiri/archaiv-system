from datetime import datetime
from flask import jsonify

ACCEPTED_DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S")  # single source of truth


def _parse_date(value):
    """Returns a datetime or None. Not exported — internal only."""
    if value is None:
        return None
    for fmt in ACCEPTED_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except (ValueError, TypeError):
            continue
    return None


def validate_date_string(value, field_name):
    """Format-only check. Used by assets/staff routes where 'in the past' is fine."""
    if value is None:
        return None
    if _parse_date(value) is None:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be a valid date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)"
                }
            ),
            400,
        )
    return None


def validate_date_fields(data, field_names):
    """Runs validate_date_string over several fields at once.
    Returns the first error response found, or None if all are valid."""
    for field in field_names:
        if field in data:
            error = validate_date_string(data.get(field), field)
            if error:
                return error
    return None


MIN_PASSWORD_LENGTH = 8


def validate_password_strength(password, field_name="password"):
    """Length-only check for now — kept simple on purpose. Tighten later
    (e.g. require a digit/letter mix) if the thesis defense calls for it."""
    if password is None or len(password) < MIN_PASSWORD_LENGTH:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be at least {MIN_PASSWORD_LENGTH} characters"
                }
            ),
            400,
        )
    return None


DEFAULT_PAGE_LIMIT = 20
MAX_PAGE_LIMIT = 100


def parse_pagination_params(args):
    """Parses and validates ?limit= and ?offset= query params.
    Returns (limit, offset, error_response) — error_response is None if valid.
    limit defaults to DEFAULT_PAGE_LIMIT and is capped at MAX_PAGE_LIMIT
    (same safety-cap idea as the audit log route). offset defaults to 0."""
    try:
        limit = int(args.get("limit", DEFAULT_PAGE_LIMIT))
    except ValueError:
        return None, None, (jsonify({"error": "limit must be an integer"}), 400)

    try:
        offset = int(args.get("offset", 0))
    except ValueError:
        return None, None, (jsonify({"error": "offset must be an integer"}), 400)

    if limit < 1:
        return None, None, (jsonify({"error": "limit must be at least 1"}), 400)
    if offset < 0:
        return None, None, (jsonify({"error": "offset cannot be negative"}), 400)

    limit = min(limit, MAX_PAGE_LIMIT)

    return limit, offset, None


def validate_due_date(value, field_name="due_date"):
    """Format + not-in-the-past, parsing once via _parse_date."""
    if value is None:
        return None
    parsed = _parse_date(value)
    if parsed is None:
        return (
            jsonify(
                {
                    "error": f"{field_name} must be a valid date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)"
                }
            ),
            400,
        )
    if parsed < datetime.now():
        return (
            jsonify({"error": f"{field_name} cannot be in the past"}),
            400,
        )
    return None


# The only tables validate_lookup_id is ever called with (see call sites in
# routes/members.py, routes/assets.py, routes/setup.py). `table` gets
# interpolated straight into SQL below since SQLite doesn't support binding
# identifiers as parameters — this allowlist is what actually keeps that
# safe, rather than relying solely on callers to remember the docstring
# rule not to pass user input as the table name.
_LOOKUP_TABLES = {"departments", "categories"}


def validate_lookup_id(conn, table, id_value, field_name, required=False):
    """Checks that id_value references an existing row in `table`.
    `table` must always be a hardcoded literal from your own code — never
    pass user input as the table name. Returns an error response, or None
    if valid (or if not required and id_value is None)."""
    if table not in _LOOKUP_TABLES:
        raise ValueError(
            f"validate_lookup_id: {table!r} is not an allowed lookup table "
            f"(add it to _LOOKUP_TABLES in utils/validators.py if this is intentional)"
        )

    if id_value is None:
        if required:
            return jsonify({"error": f"Missing required field: {field_name}"}), 400
        return None

    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (id_value,)).fetchone()
    if row is None:
        return (
            jsonify({"error": f"{field_name} does not reference an existing entry"}),
            400,
        )
    return None
