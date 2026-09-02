"""Landing page: the domains the user can see, with their forms, filterable by text."""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, html

from rdm.services import NavDomain
from rdm.ui import ids
from rdm.ui.components import ROLE_COLORS, empty_state, icon, link_button, page_title
from rdm.ui.context import AppContext, get_context, navigation
from rdm.ui.layout import domain_href, form_href


def render(ctx_: AppContext) -> dmc.Stack:
    items = navigation(ctx_, None)
    header = page_title(
        "Reference data",
        f"Welcome, {ctx_.user.label}. Pick a form from the sidebar or from the domains below. "
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
    return dmc.Stack(
        [
            header,
            stats,
            dmc.TextInput(
                id=ids.HOME_FILTER,
                placeholder="Filter domains and forms",
                leftSection=icon("tabler:filter"),
                debounce=250,
                w=360,
            ),
            html.Div(id=ids.HOME_CARDS, children=cards(items, None)),
        ],
        gap="lg",
    )


def cards(items: list[NavDomain], text: str | None) -> dmc.SimpleGrid | dmc.Paper:
    needle = (text or "").strip().lower()
    out = []
    for item in items:
        d = item.domain
        forms = item.forms
        if needle and needle not in f"{d.name} {d.title} {d.description} {d.owner}".lower():
            forms = [f for f in forms if needle in f"{f.name} {f.title} {f.description} {f.owner}".lower()]
            if not forms:
                continue
        links = [
            dmc.Anchor(
                dmc.Group([icon("tabler:table", 14), dmc.Text(f.title, size="sm")], gap=6),
                href=form_href(d.name, f.name),
                underline="never",
            )
            for f in forms
        ] or [dmc.Text("No forms yet.", size="sm", c="dimmed")]
        actions = [
            link_button(
                "Open domain",
                domain_href(d.name),
                variant="light",
                size="xs",
                leftSection=icon("tabler:folder-open", 14),
            )
        ]
        if d.doc_link:
            actions.append(
                dmc.Anchor(
                    dmc.Button(
                        "Documentation",
                        variant="subtle",
                        size="xs",
                        leftSection=icon("tabler:external-link", 14),
                    ),
                    href=d.doc_link,
                    target="_blank",
                )
            )
        out.append(
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
                        dmc.Group(actions, gap="xs"),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                padding="md",
            )
        )
    if not out:
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    return dmc.SimpleGrid(out, cols={"base": 1, "md": 2, "xl": 3}, spacing="md")


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


def register(app) -> None:
    @app.callback(
        Output(ids.HOME_CARDS, "children"),
        Input(ids.HOME_FILTER, "value"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def filter_home(text, persona):
        c = get_context(persona)
        return cards(navigation(c, None), text)
