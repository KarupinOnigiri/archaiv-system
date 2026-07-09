def full_name(first_name, last_name, middle_name=None, suffix=None):
    """Assembles a display name from separate name columns."""
    parts = [first_name, middle_name, last_name]
    name = " ".join(p for p in parts if p)
    if suffix:
        name = f"{name} {suffix}"
    return name
