"""Canonical widget catalog: names, card type, and which roles can see each one.
Shared by routes/stats.py, report_layout_service.py, and templates/reports.html.

(Bisaya: Kini ang opisyal/canonical nga listahan sa mga widget sa Reports
page - ngalan, klase sa card - stat/chart/table - ug kung unsang mga role
ang pwede makakita sa matag usa. Kini gigamit sa routes/stats.py,
report_layout_service.py, ug templates/reports.html - usa ra ka lugar
bubutangan sa impormasyon bahin sa mga widget, dili balik-balik i-type sa
lain-laing file.)
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
    """Every widget this role is allowed to see, in default order.
    (Bisaya: Tanan nga widget nga gitugotan makita ani nga role, sunod sa
    default nga han-ay.)

    Args:
        role: a staff role string, e.g. "admin", "in_charge", "tool_keeper".
        (Bisaya: usa ka role sa staff, e.g. "admin", "in_charge",
        "tool_keeper".)

    Returns:
        list[dict] - the full widget entries (id/title/type/roles) this
        role can see, in WIDGETS' own order.
        (Bisaya: usa ka listahan sa dict - ang kompleto nga entry sa
        widget - id/title/type/roles - nga makita ani nga role, sunod sa
        han-ay sa WIDGETS.)
    """
    return [w for w in WIDGETS if role in w["roles"]]


def widget_ids_for_role(role):
    """Just the ids from widgets_for_role() - what report_layout_service.py
    actually needs when building a default layout.
    (Bisaya: Ang mga id ra gikan sa widgets_for_role() - kini ang
    gikinahanglan sa report_layout_service.py kung mag-himo ug default nga
    layout.)

    Args:
        role: a staff role string.
        (Bisaya: usa ka role sa staff.)

    Returns:
        list[str] - widget ids this role can see, in default order.
        (Bisaya: listahan sa mga widget id nga makita ani nga role, sunod
        sa default nga han-ay.)
    """
    return [w["id"] for w in widgets_for_role(role)]


def is_widget_allowed(widget_id, role):
    """Whether a specific widget id is both real and visible to this role -
    the actual security check (report_layout_service.py filters a saved/
    submitted layout through this, so a staff member can't sneak an
    admin-only widget into their own layout by editing the request).
    (Bisaya: Kung tinuod ba gyud kini nga widget id UG makita ba sa maong
    role - kini ang TINUOD nga security check - gi-filter sa
    report_layout_service.py ang bisan unsang na-save/gipasa nga layout
    agi ani, para dili makasulod ang usa ka staff sa admin-only nga widget
    sa iyang kaugalingong layout pinaagi sa pag-usab sa request.)

    Args:
        widget_id: the widget id to check, or None/garbage (returns False
            safely).
        (Bisaya: ang widget id nga susihon, o None/dili tinuod nga value -
        mubalik ug False nga safe.)
        role: a staff role string.
        (Bisaya: usa ka role sa staff.)

    Returns:
        bool - True only when widget_id is a real entry in WIDGETS_BY_ID
        AND role is in that widget's allowed roles.
        (Bisaya: True ra kung tinuod nga naa sa WIDGETS_BY_ID ang
        widget_id UG naa ang role sa mga role nga gitugotan ani nga
        widget.)
    """
    widget = WIDGETS_BY_ID.get(widget_id)
    return widget is not None and role in widget["roles"]
