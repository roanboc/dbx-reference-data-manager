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
    domain_badge,
    empty_state,
    error_alert,
    function_card,
    icon,
    link_button,
    matching_objects,
    page_title,
    stat_tile,
)
from rdm.ui.context import AppContext, get_context, grouped_navigation
from rdm.ui.routes import DOMAINS_HREF


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
            stat_tile("Functions in the domain", domain.function_count or 0, "tabler:folders"),
            stat_tile("Functions you can open", len(functions), "tabler:folder-open"),
            stat_tile("Forms", n_forms, "tabler:table"),
            stat_tile("Files", n_files, "tabler:file-spreadsheet"),
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
        matched = matching_objects(item, needle)
        if matched is None:
            continue
        f = item.function
        meta = " · ".join(
            x
            for x in [
                f"{len(item.forms)} form(s)",
                f"{len(item.files)} file(s)",
                f"owner {f.owner}" if f.owner else "",
            ]
            if x
        )
        cards.append(function_card(item, *matched, meta, details=[dmc.Code(f.name)]))
    if not cards:
        if not functions:
            return empty_state(
                "No functions you can open in this domain",
                "Ask a function administrator for Viewer or Editor access, or a global admin to assign a function to this domain.",
                "tabler:lock",
            )
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    return dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3}, spacing="md")


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
