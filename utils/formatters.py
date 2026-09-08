def full_name(first_name, last_name, middle_name=None, suffix=None):
    """Mag-porma og full display name gikan sa lain-laing column.

    Returns: str - gi-join, gi-skip ang mga walay laman (middle_name/suffix)."""
    parts = [first_name, middle_name, last_name]
    name = " ".join(p for p in parts if p)
    if suffix:
        name = f"{name} {suffix}"
    return name


def humanize_label(value):
    """Mag-usab sa snake_case ngadto sa display label, e.g. 'for_repair'
    -> 'For Repair'. Jinja's `capitalize` mobiya sa underscore, mao ni
    gigamit instead.

    Returns: str (o ang original value kung walay laman/None)."""
    if not value:
        return value
    return value.replace("_", " ").title()
