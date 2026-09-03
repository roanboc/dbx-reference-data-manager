"""Help > About: what the app is for, how data is organised, the pain it removes and when
(not) to use it. Written for anyone in the organisation, with icons and simple diagrams."""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import html

from rdm.config import APP_TITLE
from rdm.ui.components import icon
from rdm.ui.context import AppContext

LEVELS = [
    (
        "tabler:sitemap",
        "indigo",
        "Domain",
        "A business area, aligned with the organisation's data domains.",
        "Finance",
    ),
    (
        "tabler:folder",
        "violet",
        "Function (sub-domain)",
        "A team or capability inside the domain. Access is granted here.",
        "Cost Management",
    ),
    (
        "tabler:table",
        "teal",
        "Objects: forms and files",
        "Forms are lists edited in a grid. Files are CSV/Parquet datasets too large for a grid.",
        "Cost Centres (form), GL Transactions (file)",
    ),
]

PAINS = [
    (
        "tabler:files",
        "Many places",
        "Spreadsheets, SharePoint lists, emails and values hard-coded in pipelines and reports.",
    ),
    ("tabler:versions", "Many versions", "Nobody is sure which copy is current, or who owns it."),
    ("tabler:history-off", "No trail", "Changes are not tracked and cannot be undone."),
    ("tabler:keyboard", "Re-keying", "The same values are typed again into every system that needs them."),
]

BENEFITS = [
    (
        "tabler:building-bank",
        "Centralised",
        "One catalog, one address per list, findable by domain and function, with a named owner.",
    ),
    (
        "tabler:shield-check",
        "Governed",
        "Access by role through Unity Catalog, validation on every edit, and a full history with restore.",
    ),
    (
        "tabler:refresh",
        "Fresh",
        "A saved change is immediately available to every report, model and pipeline that reads it. No copies.",
    ),
]

GOOD_FIT = [
    "Data that changes slowly and is maintained by people, usually in batches (daily, monthly, ad hoc).",
    "Lookups, mappings, hierarchies, thresholds, calendars, parameters.",
    "From a handful of rows (forms) to millions of rows delivered as files.",
]

NOT_FOR = [
    "Transactional or operational data written by applications or devices.",
    "High-frequency updates (many changes per minute) or real-time events and streams.",
    "Large operational tables that belong in a source system or a data pipeline.",
]

ROLES = [
    ("tabler:eye", "gray", "Viewer", "reads, filters and exports."),
    ("tabler:pencil", "blue", "Editor", "changes rows and replaces files."),
    (
        "tabler:user-cog",
        "grape",
        "Function admin",
        "creates forms and files, defines columns, grants access.",
    ),
    ("tabler:world-cog", "orange", "Global admin", "creates functions, maintains domains, deletes."),
]


def _level_card(icon_name: str, color: str, title: str, text: str, example: str) -> dmc.Paper:
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.ThemeIcon(
                            icon(icon_name, 20), size="lg", radius="md", variant="light", color=color
                        ),
                        dmc.Text(title, fw=600),
                    ],
                    gap="sm",
                    wrap="nowrap",
                ),
                dmc.Text(text, size="sm", c="dimmed"),
                dmc.Group(
                    [
                        dmc.Text("e.g.", size="xs", c="dimmed"),
                        dmc.Badge(example, variant="light", color=color),
                    ],
                    gap=6,
                ),
            ],
            gap="xs",
        ),
        withBorder=True,
        p="md",
        radius="md",
        style={"flex": "1 1 220px", "minWidth": 0},
    )


def hierarchy_diagram() -> dmc.Group:
    """Three cards joined by arrows: domain > function > objects."""
    items: list = []
    for i, level in enumerate(LEVELS):
        if i:
            items.append(
                dmc.ThemeIcon(icon("tabler:arrow-right", 18), variant="subtle", color="gray", size="md")
            )
        items.append(_level_card(*level))
    return dmc.Group(items, gap="sm", align="stretch", wrap="wrap", className="rdm-hierarchy")


def _point(icon_name: str, color: str, title: str, text: str) -> dmc.Group:
    return dmc.Group(
        [
            dmc.ThemeIcon(icon(icon_name, 18), size="md", radius="xl", variant="light", color=color),
            dmc.Stack([dmc.Text(title, fw=600, size="sm"), dmc.Text(text, size="sm", c="dimmed")], gap=0),
        ],
        gap="sm",
        align="flex-start",
        wrap="nowrap",
    )


def _check_list(items: list[str], icon_name: str, color: str) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Group(
                [icon(icon_name, 16, color=color), dmc.Text(text, size="sm")],
                gap=6,
                align="flex-start",
                wrap="nowrap",
            )
            for text in items
        ],
        gap=6,
    )


def _section(title: str, icon_name: str, children: list, intro: str | None = None) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Group([icon(icon_name, 20), dmc.Title(title, order=4)], gap="xs"),
            dmc.Text(intro, size="sm", c="dimmed") if intro else None,
            *children,
        ],
        gap="sm",
    )


def render(ctx_: AppContext) -> dmc.Stack:
    contact = (ctx_.settings.admin_contact or "").strip()
    if contact.startswith("http"):
        contact_node = dmc.Anchor(contact, href=contact, target="_blank")
    elif "@" in contact:
        contact_node = dmc.Anchor(contact, href=f"mailto:{contact}")
    elif contact:
        contact_node = dmc.Text(contact, span=True, fw=600)
    else:
        contact_node = dmc.Text(
            "your Reference Data administrator (a global admin, see Your access at the bottom of the sidebar)",
            span=True,
        )
    return dmc.Stack(
        [
            dmc.Paper(
                dmc.Group(
                    [
                        dmc.ThemeIcon(
                            icon("tabler:table-options", 30), size=56, radius="md", variant="light"
                        ),
                        dmc.Stack(
                            [
                                dmc.Title(f"About the {APP_TITLE}", order=3),
                                dmc.Text(
                                    "One governed home for the reference data your reports, models and pipelines "
                                    "depend on: codes, mappings, hierarchies, rates and parameters that give meaning "
                                    "to the rest of the data and are maintained by the people who know the business.",
                                    size="sm",
                                ),
                            ],
                            gap=4,
                        ),
                    ],
                    align="flex-start",
                    wrap="nowrap",
                ),
                p="md",
                radius="md",
                bg="var(--mantine-primary-color-light)",
            ),
            _section(
                "How the data is organised",
                "tabler:hierarchy-2",
                [
                    hierarchy_diagram(),
                    dmc.Group(
                        [
                            dmc.Text("Example path:", size="xs", c="dimmed"),
                            dmc.Code("Finance  ›  Cost Management  ›  Cost Centres"),
                        ],
                        gap=6,
                    ),
                ],
                "Every list has one place in a three-level tree, so it is easy to find and clear who looks after it.",
            ),
            dmc.SimpleGrid(
                [
                    dmc.Paper(
                        _section(
                            "The problem today",
                            "tabler:alert-triangle",
                            [_point(i, "red", t, d) for i, t, d in PAINS],
                        ),
                        withBorder=True,
                        p="md",
                        radius="md",
                    ),
                    dmc.Paper(
                        _section(
                            "What the app gives you",
                            "tabler:sparkles",
                            [_point(i, "teal", t, d) for i, t, d in BENEFITS],
                        ),
                        withBorder=True,
                        p="md",
                        radius="md",
                    ),
                ],
                cols={"base": 1, "md": 2},
                spacing="md",
            ),
            _section(
                "Is it the right tool?",
                "tabler:scale",
                [
                    dmc.SimpleGrid(
                        [
                            dmc.Paper(
                                dmc.Stack(
                                    [
                                        dmc.Group(
                                            [
                                                icon("tabler:circle-check", 18, color="teal"),
                                                dmc.Text("A good fit", fw=600),
                                            ],
                                            gap=6,
                                        ),
                                        _check_list(GOOD_FIT, "tabler:check", "teal"),
                                    ],
                                    gap="xs",
                                ),
                                withBorder=True,
                                p="md",
                                radius="md",
                            ),
                            dmc.Paper(
                                dmc.Stack(
                                    [
                                        dmc.Group(
                                            [
                                                icon("tabler:circle-x", 18, color="red"),
                                                dmc.Text("Not designed for", fw=600),
                                            ],
                                            gap=6,
                                        ),
                                        _check_list(NOT_FOR, "tabler:x", "red"),
                                    ],
                                    gap="xs",
                                ),
                                withBorder=True,
                                p="md",
                                radius="md",
                            ),
                        ],
                        cols={"base": 1, "md": 2},
                        spacing="md",
                    ),
                    dmc.Alert(
                        html.Span(["Something else in mind, or not sure? Talk to ", contact_node, "."]),
                        title="Other use cases",
                        color="indigo",
                        variant="light",
                        icon=icon("tabler:message-circle-question"),
                    ),
                ],
                "The app is built for data that changes slowly and normally in batches, not for transactional needs.",
            ),
            _section(
                "Who does what",
                "tabler:users",
                [
                    dmc.SimpleGrid(
                        [
                            dmc.Group(
                                [
                                    dmc.ThemeIcon(
                                        icon(i, 16), size="sm", radius="xl", variant="light", color=c
                                    ),
                                    dmc.Text([html.B(f"{r}: "), d], size="sm"),
                                ],
                                gap=6,
                                wrap="nowrap",
                                align="flex-start",
                            )
                            for i, c, r, d in ROLES
                        ],
                        cols={"base": 1, "md": 2},
                        spacing="xs",
                    )
                ],
                "Roles are given per function, so each team controls its own lists.",
            ),
            _section(
                "Where the data lives",
                "tabler:database",
                [
                    dmc.Text(
                        [
                            "Everything is stored in Databricks Unity Catalog, in the ",
                            dmc.Code(ctx_.settings.catalog),
                            " catalog: every form is a table and every file sits in the function's volume. Analysts, "
                            "models and pipelines read them directly, and each change is recorded in a history you "
                            "can browse and restore from.",
                        ],
                        size="sm",
                    )
                ],
            ),
        ],
        gap="lg",
    )
