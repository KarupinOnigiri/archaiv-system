def full_name(first_name, last_name, middle_name=None, suffix=None):
    """Assembles a display name from separate name columns."""
    parts = [first_name, middle_name, last_name]
    name = " ".join(p for p in parts if p)
    if suffix:
        name = f"{name} {suffix}"
    return name


def humanize_label(value):
    """Turns a snake_case value into a display label, e.g. 'for_repair' -> 'For Repair'.
    Jinja's `capitalize` leaves the underscore in place, so it rendered "For_repair"."""
    if not value:
        return value
    return value.replace("_", " ").title()
