"""Domain overview: one domain, its details and the functions assigned to it (with their forms and files).

Everyone sees the domain list; the functions shown are the ones the signed-in user may open.
Editing the domain itself happens on the Domains page (global admins).
"""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, dcc, html

from rdm.backend.base import NotFoundError
from rdm.services import NavFunction
from rdm.ui import ids
from rdm.ui.components import (
    ROLE_COLORS,
    domain_badge,
    empty_state,
    error_alert,
    icon,
    link_button,
    page_title,
)
from rdm.ui.context import AppContext, get_context, grouped_navigation
from rdm.ui.layout import DOMAINS_HREF, file_href, form_href, function_href


def _functions_of(ctx_: AppContext, domain: str) -> list[NavFunction]:
    for group in grouped_navigation(ctx_, None):
        if group.domain.name == domain:
            return group.functions
    return []


def render(ctx_: AppContext, domain_name: str) -> dmc.Stack:
    try:
        domain = ctx_.backend.get_domain(domain_name)
    except NotFoundError as exc:
        return dmc.Stack([error_alert(str(exc), "Not found")])
    functions = _functions_of(ctx_, domain.name)
    n_forms = sum(len(f.forms) for f in functions)
    n_files = sum(len(f.files) for f in functions)
    right = [domain_badge("Domain")]
    if ctx_.permissions.can_manage_domains:
        right.append(
            link_button(
                "Manage domains",
                DOMAINS_HREF,
                variant="subtle",
                size="xs",
                leftSection=icon("tabler:settings", 14),
            )
        )
    header = page_title(
        domain.title,
        domain.description or None,
        crumbs=[dmc.Anchor("Home", href="/"), dmc.Text("Domain")],
        right=dmc.Stack(
            [
                dmc.Group(right, gap="xs", justify="flex-end"),
                dmc.Text(f"owner {domain.owner}" if domain.owner else "", size="xs", c="dimmed", ta="right"),
            ],
            gap=4,
            align="flex-end",
        ),
    )
    stats = dmc.SimpleGrid(
        [
            _stat("Functions in the domain", domain.function_count or 0, "tabler:folders"),
            _stat("Functions you can open", len(functions), "tabler:folder-open"),
            _stat("Forms", n_forms, "tabler:table"),
            _stat("Files", n_files, "tabler:file-spreadsheet"),
        ],
        cols={"base": 2, "sm": 4},
    )
    hidden = (domain.function_count or 0) - len(functions)
    note = (
        dmc.Text(
            f"{hidden} function(s) of this domain are not shown because you have no access to them.",
            size="xs",
            c="dimmed",
        )
        if hidden > 0
        else None
    )
    return dmc.Stack(
        [
            dcc.Store(id=ids.DOMAIN_KEY, data=domain.name),
            header,
            stats,
            dmc.Group(
                [
                    dmc.Title("Functions", order=4),
                    dmc.TextInput(
                        id=ids.DOMAIN_FUNCTIONS_FILTER,
                        placeholder="Filter functions, forms and files",
                        leftSection=icon("tabler:filter"),
                        debounce=250,
                        w=320,
                        size="sm",
                    ),
                ],
                justify="space-between",
            ),
            note,
            html.Div(id=ids.DOMAIN_FUNCTIONS, children=function_cards(functions, None)),
        ],
        gap="md",
    )


def function_cards(functions: list[NavFunction], text: str | None) -> dmc.SimpleGrid | dmc.Paper:
    needle = (text or "").strip().lower()
    cards = []
    for item in functions:
        f = item.function
        forms, files = item.forms, item.files
        if needle and needle not in f"{f.name} {f.title} {f.description} {f.owner}".lower():
            forms = [x for x in forms if needle in f"{x.name} {x.title} {x.description}".lower()]
            files = [x for x in files if needle in f"{x.name} {x.title} {x.description}".lower()]
            if not forms and not files:
                continue
        links = [
            dmc.Anchor(
                dmc.Group([icon("tabler:table", 14), dmc.Text(x.title, size="sm")], gap=6),
                href=form_href(f.name, x.name),
                underline="never",
            )
            for x in forms
        ] + [
            dmc.Anchor(
                dmc.Group(
                    [
                        icon("tabler:file-spreadsheet" if x.format == "csv" else "tabler:file-database", 14),
                        dmc.Text(x.title, size="sm"),
                        dmc.Badge(x.format.upper(), size="xs", variant="outline", color="gray"),
                    ],
                    gap=6,
                ),
                href=file_href(f.name, x.name),
                underline="never",
            )
            for x in files
        ] or [dmc.Text("No forms or files yet.", size="sm", c="dimmed")]
        cards.append(
            dmc.Card(
                dmc.Stack(
                    [
                        dmc.Group(
                            [
                                dmc.Group([icon("tabler:folder", 18), dmc.Text(f.title, fw=600)], gap=6),
                                dmc.Badge(
                                    item.role.label, color=ROLE_COLORS[item.role], variant="light", size="sm"
                                ),
                            ],
                            justify="space-between",
                        ),
                        dmc.Code(f.name),
                        dmc.Text(f.description or "No description", size="sm", c="dimmed"),
                        dmc.Text(
                            " · ".join(
                                x
                                for x in [
                                    f"{len(item.forms)} form(s)",
                                    f"{len(item.files)} file(s)",
                                    f"owner {f.owner}" if f.owner else "",
                                ]
                                if x
                            ),
                            size="xs",
                            c="dimmed",
                        ),
                        dmc.Stack(links, gap=4),
                        dmc.Group(
                            [
                                link_button(
                                    "Open function",
                                    function_href(f.name),
                                    variant="light",
                                    size="xs",
                                    leftSection=icon("tabler:folder-open", 14),
                                ),
                                dmc.Anchor(
                                    dmc.Button(
                                        "Documentation",
                                        variant="subtle",
                                        size="xs",
                                        leftSection=icon("tabler:external-link", 14),
                                    ),
                                    href=f.doc_link,
                                    target="_blank",
                                )
                                if f.doc_link
                                else None,
                            ],
                            gap="xs",
                        ),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                padding="md",
            )
        )
    if not cards:
        if not functions:
            return empty_state(
                "No functions you can open in this domain",
                "Ask a function administrator for Viewer or Editor access, or a global admin to assign a function to this domain.",
                "tabler:lock",
            )
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    return dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3}, spacing="md")


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
        Output(ids.DOMAIN_FUNCTIONS, "children"),
        Input(ids.DOMAIN_FUNCTIONS_FILTER, "value"),
        State(ids.DOMAIN_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def filter_functions(text, domain, persona):
        return function_cards(_functions_of(get_context(persona), domain), text)
