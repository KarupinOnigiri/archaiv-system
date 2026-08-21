"""Canonical widget catalog: names, card type, and which roles can see each one.
Shared by routes/stats.py, report_layout_service.py, and templates/reports.html."""

# Order sets the default widget order for a fresh install.
ADMIN_IN_CHARGE = ("admin", "in_charge")

WIDGETS = [
    # admin/in_charge only -- these are system-wide totals, not scoped to one staff member.
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
    """Every widget this role is allowed to see, in default order."""
    return [w for w in WIDGETS if role in w["roles"]]


def widget_ids_for_role(role):
    return [w["id"] for w in widgets_for_role(role)]


def is_widget_allowed(widget_id, role):
    widget = WIDGETS_BY_ID.get(widget_id)
    return widget is not None and role in widget["roles"]
