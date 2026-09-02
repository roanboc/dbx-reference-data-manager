"""Landing page: the domains the user can see, with their forms."""

from __future__ import annotations

import dash_mantine_components as dmc

from rdm.ui.components import ROLE_COLORS, empty_state, icon, link_button, page_title
from rdm.ui.context import AppContext, navigation
from rdm.ui.layout import domain_href, form_href


def render(ctx: AppContext) -> dmc.Stack:
    items = navigation(ctx, None)
    header = page_title(
        "Reference data",
        f"Welcome, {ctx.user.label}. Pick a form from the sidebar or from the domains below. "
        "Forms are editable grids backed by governed tables; every change is recorded.",
    )
    if not items:
        return dmc.Stack(
            [
                header,
                empty_state(
                    "No domains available",
                    "You have not been granted access to any domain. Ask a domain administrator for Viewer or Editor access.",
                    "tabler:lock",
                ),
            ]
        )
    n_forms = sum(len(i.forms) for i in items)
    editable = sum(len(i.forms) for i in items if i.role.can_edit)
    stats = dmc.SimpleGrid(
        [
            _stat("Domains", len(items), "tabler:folders"),
            _stat("Forms", n_forms, "tabler:table"),
            _stat("You can edit", editable, "tabler:pencil"),
        ],
        cols={"base": 1, "sm": 3},
    )
    cards = []
    for item in items:
        d = item.domain
        links = [
            dmc.Anchor(
                dmc.Group([icon("tabler:table", 14), dmc.Text(f.title, size="sm")], gap=6),
                href=form_href(d.name, f.name),
                underline="never",
            )
            for f in item.forms
        ] or [dmc.Text("No forms yet.", size="sm", c="dimmed")]
        cards.append(
            dmc.Card(
                dmc.Stack(
                    [
                        dmc.Group(
                            [
                                dmc.Group([icon("tabler:folder", 18), dmc.Text(d.title, fw=600)], gap=6),
                                dmc.Badge(
                                    item.role.label, color=ROLE_COLORS[item.role], variant="light", size="sm"
                                ),
                            ],
                            justify="space-between",
                        ),
                        dmc.Text(d.description or "No description", size="sm", c="dimmed"),
                        dmc.Text(f"Owner: {d.owner}" if d.owner else "", size="xs", c="dimmed"),
                        dmc.Stack(links, gap=4),
                        link_button(
                            "Open domain",
                            domain_href(d.name),
                            variant="light",
                            size="xs",
                            leftSection=icon("tabler:folder-open", 14),
                        ),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                padding="md",
            )
        )
    return dmc.Stack(
        [header, stats, dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3}, spacing="md")], gap="lg"
    )


def _stat(label: str, value: int, icon_name: str) -> dmc.Paper:
    return dmc.Paper(
        dmc.Group(
            [
                dmc.ThemeIcon(icon(icon_name, 20), size="lg", radius="md", variant="light"),
                dmc.Stack(
                    [dmc.Text(label, size="xs", c="dimmed"), dmc.Text(f"{value:,}", fw=700, size="xl")], gap=0
                ),
            ]
        ),
        withBorder=True,
        p="md",
        radius="md",
    )
