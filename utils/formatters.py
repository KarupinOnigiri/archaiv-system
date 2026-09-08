def full_name(first_name, last_name, middle_name=None, suffix=None):
    """Assembles a display name from separate name columns.
    (Bisaya: Mag-porma ug tibuok/full name gikan sa lain-laing column - first
    name, middle name, last name, suffix - nga naa sa database.)

    Args:
        first_name: given name, required.
        (Bisaya: pangalan, kinahanglan gyud naa.)
        last_name: family name, required.
        (Bisaya: apelyido, kinahanglan gyud naa.)
        middle_name: optional middle name (or middle initial).
        (Bisaya: middle name, pwede walay laman.)
        suffix: optional suffix like "Jr." or "III".
        (Bisaya: suffix pareha sa "Jr." o "III", pwede walay laman.)

    Returns:
        str - the parts joined with single spaces, skipping any that are
        empty/None (e.g. no middle_name just closes the gap instead of
        leaving a double space).
        (Bisaya: usa ka string - gi-tapok ang mga parte nga naay laman lang,
        gilaktawan ang walay laman aron dili mag-double space.)
    """
    parts = [first_name, middle_name, last_name]
    name = " ".join(p for p in parts if p)
    if suffix:
        name = f"{name} {suffix}"
    return name


def humanize_label(value):
    """Turns a snake_case value into a display label, e.g. 'for_repair' -> 'For Repair'.
    Jinja's `capitalize` leaves the underscore in place, so it rendered "For_repair".
    (Bisaya: Mag-usab sa snake_case nga value ngadto sa porma nga sayon
    basahon sa tawo, e.g. 'for_repair' mahimong 'For Repair'. Gigamit ni
    imbes ang Jinja's `capitalize` kay kadto usa ra gyud ka letra ang
    ge-capitalize, mag-gawas pa gihapon ang underscore - "For_repair".)

    Args:
        value: the raw snake_case string (e.g. an enum-like DB column value
            such as "for_repair" or "in_charge"). Falsy values (None, "")
            pass through unchanged.
        (Bisaya: ang orihinal nga snake_case nga string - pananglitan usa ka
        value gikan sa database sama sa "for_repair" o "in_charge". Kung
        walay laman/None, mobalik ra gihapon nga wala giusab.)

    Returns:
        str (or the original falsy value) - underscores replaced with
        spaces, each word capitalized ("for_repair" -> "For Repair").
        (Bisaya: usa ka string - ang mga underscore giilisan ug space, ug
        matag pulong gi-capitalize - "for_repair" mahimong "For Repair".)
    """
    if not value:
        return value
    return value.replace("_", " ").title()
