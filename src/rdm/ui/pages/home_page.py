"""Landing page: the functions the user can see grouped by domain, with their forms, filterable by text."""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import Input, Output, State, html

from rdm.services import NavDomain
from rdm.ui import ids
from rdm.ui.components import ROLE_COLORS, empty_state, icon, link_button, page_title
from rdm.ui.context import AppContext, get_context, grouped_navigation
from rdm.ui.layout import domain_href, file_href, form_href, function_href


def _intro() -> dmc.Paper:
    return dmc.Paper(
        dmc.Group(
            [
                dmc.Stack(
                    [
                        dmc.Text("What this app is for", fw=600, size="sm"),
                        dmc.Text(
                            "Reference Data Manager is the governed home for the lists the organisation "
                            "relies on — cost centres, codes, mappings — that used to live in spreadsheets "
                            "and SharePoint lists. Each list is a form: an editable grid backed by a Unity "
                            "Catalog table, with an owner, a description, access by role and a full history "
                            "of who changed what. Larger datasets are kept as files you can preview, "
                            "download and replace.",
                            size="sm",
                            c="dimmed",
                        ),
                    ],
                    gap=4,
                ),
                link_button(
                    "Help",
                    "/help",
                    variant="light",
                    size="xs",
                    leftSection=icon("tabler:help-circle", 14),
                ),
            ],
            justify="space-between",
            align="flex-start",
            wrap="nowrap",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )


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
                _intro(),
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
            _stat("Domains", len([g for g in groups if not g.is_unassigned]), "tabler:sitemap"),
            _stat("Functions", len(items), "tabler:folders"),
            _stat("Forms", n_forms, "tabler:table"),
            _stat("Files", n_files, "tabler:file-spreadsheet"),
            _stat("You can edit", editable, "tabler:pencil"),
        ],
        cols={"base": 2, "sm": 5},
    )
    return dmc.Stack(
        [
            header,
            _intro(),
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
        domain_matches = needle and needle in f"{d.name} {d.title} {d.description} {d.owner}".lower()
        out = []
        for item in group.functions:
            f = item.function
            forms = item.forms
            files = item.files
            if (
                needle
                and not domain_matches
                and needle not in f"{f.name} {f.title} {f.description} {f.owner}".lower()
            ):
                forms = [
                    x for x in forms if needle in f"{x.name} {x.title} {x.description} {x.owner}".lower()
                ]
                files = [
                    x for x in files if needle in f"{x.name} {x.title} {x.description} {x.owner}".lower()
                ]
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
                            icon(
                                "tabler:file-spreadsheet" if x.format == "csv" else "tabler:file-database", 14
                            ),
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
            actions = [
                link_button(
                    "Open function",
                    function_href(f.name),
                    variant="light",
                    size="xs",
                    leftSection=icon("tabler:folder-open", 14),
                )
            ]
            if f.doc_link:
                actions.append(
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
                )
            out.append(
                dmc.Card(
                    dmc.Stack(
                        [
                            dmc.Group(
                                [
                                    dmc.Group([icon("tabler:folder", 18), dmc.Text(f.title, fw=600)], gap=6),
                                    dmc.Badge(
                                        item.role.label,
                                        color=ROLE_COLORS[item.role],
                                        variant="light",
                                        size="sm",
                                    ),
                                ],
                                justify="space-between",
                            ),
                            dmc.Text(f.description or "No description", size="sm", c="dimmed"),
                            dmc.Text(f"Owner: {f.owner}" if f.owner else "", size="xs", c="dimmed"),
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
        return cards(grouped_navigation(c, None), text)
