"""Landing page: the functions the user can see grouped by domain, with their forms, filterable by text."""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, html

from rdm.services import NavDomain
from rdm.ui import ids
from rdm.ui.components import empty_state, function_card, icon, matching_objects, page_title, stat_tile
from rdm.ui.context import AppContext, get_context, grouped_navigation
from rdm.ui.routes import domain_href


def render(ctx_: AppContext) -> dmc.Stack:
    groups = grouped_navigation(ctx_, None)
    header = page_title(
        "Reference data",
        f"Welcome, {ctx_.user.label}. Pick a form from the sidebar or from the functions below. "
        "Functions are grouped by domain; forms are editable grids backed by governed tables and every "
        "change is recorded.",
    )
    if not groups:
        return dmc.Stack(
            [
                header,
                empty_state(
                    "No functions available",
                    "You have not been granted access to any function. Ask a function administrator for Viewer or Editor access.",
                    "tabler:lock",
                ),
            ]
        )
    items = [i for g in groups for i in g.functions]
    n_forms = sum(len(i.forms) for i in items)
    n_files = sum(len(i.files) for i in items)
    editable = sum(len(i.forms) + len(i.files) for i in items if i.role.can_edit)
    stats = dmc.SimpleGrid(
        [
            stat_tile("Domains", len([g for g in groups if not g.is_unassigned]), "tabler:sitemap"),
            stat_tile("Functions", len(items), "tabler:folders"),
            stat_tile("Forms", n_forms, "tabler:table"),
            stat_tile("Files", n_files, "tabler:file-spreadsheet"),
            stat_tile("You can edit", editable, "tabler:pencil"),
        ],
        cols={"base": 2, "sm": 5},
    )
    return dmc.Stack(
        [
            header,
            stats,
            dmc.TextInput(
                id=ids.HOME_FILTER,
                placeholder="Filter domains, functions, forms and files",
                leftSection=icon("tabler:filter"),
                debounce=250,
                w=360,
            ),
            html.Div(id=ids.HOME_CARDS, children=cards(groups, None)),
        ],
        gap="lg",
    )


def cards(groups: list[NavDomain], text: str | None) -> dmc.Stack | dmc.Paper:
    needle = (text or "").strip().lower()
    sections = []
    for group in groups:
        d = group.domain
        domain_matches = bool(needle) and needle in f"{d.name} {d.title} {d.description} {d.owner}".lower()
        out = []
        for item in group.functions:
            matched = (item.forms, item.files) if domain_matches else matching_objects(item, needle)
            if matched is None:
                continue
            f = item.function
            out.append(function_card(item, *matched, f"owner {f.owner}" if f.owner else ""))
        if not out:
            continue
        sections.append(
            dmc.Stack(
                [
                    dmc.Group(
                        [
                            icon(
                                "tabler:sitemap" if not group.is_unassigned else "tabler:folder-question", 18
                            ),
                            dmc.Anchor(
                                dmc.Title(d.title, order=4),
                                href=domain_href(d.name),
                                underline="never",
                                c="inherit",
                            )
                            if not group.is_unassigned
                            else dmc.Title(d.title, order=4),
                            dmc.Text(d.description or "", size="sm", c="dimmed"),
                        ],
                        gap="sm",
                        align="baseline",
                    ),
                    dmc.SimpleGrid(out, cols={"base": 1, "md": 2, "xl": 3}, spacing="md"),
                ],
                gap="xs",
            )
        )
    if not sections:
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    return dmc.Stack(sections, gap="lg")


def register(app) -> None:
    @app.callback(
        Output(ids.HOME_CARDS, "children"),
        Input(ids.HOME_FILTER, "value"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def filter_home(text, persona):
        c = get_context(persona)
        return cards(grouped_navigation(c, None), text)
