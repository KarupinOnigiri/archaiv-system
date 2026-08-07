def full_name(first_name, last_name, middle_name=None, suffix=None):
    """Assembles a display name from separate name columns."""
    parts = [first_name, middle_name, last_name]
    name = " ".join(p for p in parts if p)
    if suffix:
        name = f"{name} {suffix}"
    return name


def humanize_label(value):
    """Turns a snake_case DB value (e.g. asset.condition's 'for_repair')
    into a display label ('For Repair'). Plain Jinja `capitalize` only
    touches the first character and leaves the underscore in place --
    fine for single-word values like 'functional' or 'disposed', but it
    rendered the literal string "For_repair" everywhere condition was
    shown, since that's the one condition value with more than one word.
    Safe to use on any snake_case enum-like column, not just condition --
    single-word values pass through capitalize's usual behavior unchanged."""
    if not value:
        return value
    return value.replace("_", " ").title()
