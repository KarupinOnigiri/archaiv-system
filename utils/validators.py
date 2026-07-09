from datetime import datetime
from flask import jsonify


def validate_date_string(value, field_name):
    if value is None:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            datetime.strptime(value, fmt)
            return None
        except (ValueError, TypeError):
            continue
    return (
        jsonify(
            {
                "error": f"{field_name} must be a valid date (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)"
            }
        ),
        400,
    )
