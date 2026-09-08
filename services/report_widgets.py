"""Canonical widget catalog para sa Reports page - ngalan, klase sa card
(stat/chart/table), ug kung unsang role makakita matag usa. Shared sa
routes/stats.py, report_layout_service.py, ug templates/reports.html.
"""

# Order sets the default widget order for a fresh install.
ADMIN_IN_CHARGE = ("admin", "in_charge")

WIDGETS = [
    # admin/in_charge only - these are system-wide totals, not scoped to one staff member.
    {
        "id": "summary",
        "title": "At-a-glance summary",
        "type": "stat",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "most_borrowed_assets",
        "title": "Most Borrowed Assets",
        "type": "chart",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "most_borrowed_categories",
        "title": "Most Borrowed Categories",
        "type": "chart",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "top_borrowers",
        "title": "Top Borrowers",
        "type": "chart",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "least_borrowed_assets",
        "title": "Least Borrowed Assets",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "avg_loan_duration",
        "title": "Average Loan Duration",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "overdue_frequency",
        "title": "Overdue Frequency",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "condition_changes",
        "title": "Condition Changes",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "staff_activity",
        "title": "Staff Activity",
        "type": "table",
        "roles": ("admin",),
    },
    # Financial data - excluded from tool_keeper, same reasoning as staff_activity.
    {
        "id": "asset_valuation",
        "title": "Asset Valuation",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    {
        "id": "maintenance_schedule",
        "title": "Maintenance Schedule",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    # Summary of the RFID exit-monitoring log for the report's date range -
    # the detailed/interactive version lives on Monitoring's own Trends tab,
    # this is a printable/exportable summary alongside the rest of Reports.
    # Hidden (routes/stats.py's _feature_disabled_widget_ids) whenever the
    # "monitoring" feature itself is off, on top of this normal role gate.
    {
        "id": "monitoring_activity",
        "title": "Monitoring Activity",
        "type": "table",
        "roles": ADMIN_IN_CHARGE,
    },
    # Tool Keeper sees only these three, each scoped to their own activity.
    {
        "id": "my_checkouts",
        "title": "My Checkouts Processed",
        "type": "table",
        "roles": ("tool_keeper",),
    },
    {
        "id": "my_returns",
        "title": "My Returns Processed",
        "type": "table",
        "roles": ("tool_keeper",),
    },
    {
        "id": "my_overdue_handled",
        "title": "My Overdue Handled",
        "type": "table",
        "roles": ("tool_keeper",),
    },
]

WIDGETS_BY_ID = {w["id"]: w for w in WIDGETS}


def widgets_for_role(role):
    """Tanan nga widget nga gitugotan makita ani nga role, sunod sa
    default nga han-ay.

    Returns: list[dict] - full widget entries."""
    return [w for w in WIDGETS if role in w["roles"]]


def widget_ids_for_role(role):
    """Ang mga id ra gikan sa widgets_for_role() - gikinahanglan sa
    report_layout_service.py para mag-himo ug default layout.

    Returns: list[str] - widget ids."""
    return [w["id"] for w in widgets_for_role(role)]


def is_widget_allowed(widget_id, role):
    """Kung tinuod ba nga widget id UG visible sa maong role - kini ang
    TINUOD nga security check, gigamit sa report_layout_service.py aron
    dili makasulod ang usa ka staff sa admin-only widget pinaagi sa
    pag-usab sa request.

    Args:
        widget_id: susihon, mubalik False nga safe kung None/garbage.
        role: a staff role string.

    Returns: bool - True ra kung naa sa WIDGETS_BY_ID UG naa ang role sa
        gitugotan.
    """
    widget = WIDGETS_BY_ID.get(widget_id)
    return widget is not None and role in widget["roles"]
