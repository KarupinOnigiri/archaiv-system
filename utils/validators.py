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
