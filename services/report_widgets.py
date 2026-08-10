"""Canonical catalog of every Reports-page widget: what it's called, what
kind of card it renders as, and which roles are even allowed to have it on
their dashboard. Single source of truth shared by:
  - routes/stats.py       -- serves this (role-filtered) to the frontend,
                              and validates `sections=` on /stats/export
                              against it so a role can't export a widget
                              it isn't allowed to see.
  - services/report_layout_service.py -- merges a staff member's saved
                              order/visibility with this list, dropping any
                              widget id the registry (or their role) no
                              longer recognizes.
  - templates/reports.html -- one <div data-widget-id> per entry, and the
                              Customize panel's checklist.

Kept in its own module (rather than living in routes/stats.py) so
report_layout_service.py can import it without a routes -> services ->
routes circular import.
"""

# Every role that can ever see Reports at all. Order here also sets the default widget order
# (orientation numbers first, ranked lists next, detail tables last) for a fresh install.
ADMIN_IN_CHARGE = ("admin", "in_charge")

WIDGETS = [
    # admin/in_charge only, NOT tool_keeper -- these are system-wide totals that would break
    # "own activity only" scoping if shown to tool_keeper; matches /stats/summary's own gating.
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
    # Tool Keeper's Reports access is deliberately "own activity only" -- no cross-staff
    # comparisons. These three widgets are the only ones a tool_keeper can see; every
    # query behind them filters to g.staff["id"] regardless of the request.
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
