"""Domain administration (global admins): the list of business domains that group functions.

Domains are the top of the hierarchy (domain > function > form). They mirror the
organisation's data domains (the classification Databricks calls *domains*); the app records
each function's domain as a schema property and tag and keeps the list in the registry.
"""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import DomainDef, sanitize_identifier
from rdm.ui import ids
from rdm.ui.components import error_alert, icon, info_alert, link_button, notify, page_title
from rdm.ui.context import AppContext, get_context, invalidate_metadata
from rdm.ui.routes import domain_href

CREATE_TITLE = "New domain"


def render(ctx_: AppContext) -> dmc.Stack:
    header = page_title(
        "Domains",
        "Business domains group the functions (schemas) of the catalog: domain > function > form. "
        "Keep this list aligned with the organisation's data domains.",
        crumbs=[dmc.Anchor("Home", href="/"), dmc.Text("Domains")],
    )
    if not ctx_.permissions.can_manage_domains:
        return dmc.Stack(
            [
                header,
                info_alert(
                    "Only global administrators maintain the domain list. Everyone sees domains in the sidebar.",
                    "Global admins only",
                    "yellow",
                ),
            ]
        )
    return dmc.Stack(
        [
            dcc.Store(id=ids.DOMAIN_EDITING, data=None),
            header,
            dmc.SimpleGrid(
                [
                    dmc.Paper(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [
                                        dmc.Title("Domains", order=4),
                                        dmc.TextInput(
                                            id=ids.DOMAINS_FILTER,
                                            placeholder="Filter domains",
                                            leftSection=icon("tabler:filter"),
                                            debounce=250,
                                            w=240,
                                            size="sm",
                                        ),
                                    ],
                                    justify="space-between",
                                ),
                                html.Div(
                                    id=ids.DOMAINS_TABLE,
                                    children=domains_table(ctx_, None),
                                    style={
                                        "overflowX": "auto"
                                    },  # a wide table scrolls, never overlaps the form
                                ),
                            ],
                            gap="sm",
                        ),
                        withBorder=True,
                        p="md",
                        radius="md",
                    ),
                    dmc.Paper(_form(ctx_), withBorder=True, p="md", radius="md"),
                ],
                cols={"base": 1, "lg": 2},
                spacing="md",
            ),
        ],
        gap="md",
    )


def _form(ctx_: AppContext) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Title(CREATE_TITLE, order=4, id=ids.DOMAIN_FORM_TITLE),
            dmc.TextInput(
                id=ids.DOMAIN_NAME,
                label="Name",
                placeholder="e.g. student",
                description="Identifier: lower_snake_case, becomes the value of the rdm.domain schema property; cannot change",
                required=True,
            ),
            dmc.TextInput(id=ids.DOMAIN_DISPLAY, label="Display name", placeholder="Student"),
            dmc.Textarea(id=ids.DOMAIN_DESC, label="Description", autosize=True, minRows=2),
            dmc.TextInput(
                id=ids.DOMAIN_OWNER,
                label="Owner",
                value=ctx_.user.email or ctx_.user.username,
                description="Domain owner (team or person)",
            ),
            dmc.Group(
                [
                    dmc.Button("Create domain", id=ids.DOMAIN_SUBMIT, leftSection=icon("tabler:plus")),
                    dmc.Button("Cancel", id=ids.DOMAIN_CANCEL, variant="subtle", color="gray"),
                ]
            ),
            html.Div(id=ids.DOMAIN_RESULT),
            html.Div(
                id=ids.DOMAIN_DELETE_ZONE,
                children=dmc.Stack(
                    [
                        dmc.Divider(),
                        dmc.Title("Delete domain", order=5, c="red"),
                        dmc.Text(
                            "A domain can only be deleted when no function is assigned to it.",
                            size="sm",
                            c="dimmed",
                        ),
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=ids.DOMAIN_DELETE_CONFIRM,
                                    placeholder="type the domain name to confirm",
                                    w=300,
                                ),
                                dmc.Button(
                                    "Delete",
                                    id=ids.DOMAIN_DELETE_SUBMIT,
                                    color="red",
                                    variant="light",
                                    leftSection=icon("tabler:trash-x"),
                                ),
                            ],
                            align="flex-end",
                        ),
                    ],
                    gap="xs",
                ),
                style={"display": "none"},
            ),
        ],
        gap="sm",
    )


def domains_table(ctx_: AppContext, text: str | None) -> dmc.Table | dmc.Text:
    domains = ctx_.forms.list_domains()
    needle = (text or "").strip().lower()
    shown = [
        d for d in domains if not needle or needle in f"{d.name} {d.title} {d.description} {d.owner}".lower()
    ]
    if not shown:
        return dmc.Text(
            "No domains yet. Create the first one on the right." if not domains else "No matching domains.",
            size="sm",
            c="dimmed",
        )
    rows = []
    for d in shown:
        rows.append(
            dmc.TableTr(
                [
                    dmc.TableTd(
                        dmc.Stack(
                            [
                                dmc.Group(
                                    [icon("tabler:sitemap", 16), dmc.Text(d.title, size="sm", fw=600)], gap=6
                                ),
                                dmc.Text(d.name, size="xs", c="dimmed"),
                            ],
                            gap=0,
                        )
                    ),
                    dmc.TableTd(dmc.Text(d.description or "", size="sm", c="dimmed", lineClamp=2)),
                    dmc.TableTd(dmc.Text(d.owner or "", size="sm")),
                    dmc.TableTd(dmc.Badge(f"{d.function_count or 0}", variant="light", size="sm")),
                    dmc.TableTd(
                        dmc.Group(
                            [
                                link_button(
                                    "Open",
                                    domain_href(d.name),
                                    size="xs",
                                    variant="subtle",
                                    leftSection=icon("tabler:external-link", 14),
                                ),
                                dmc.Button(
                                    "Edit",
                                    id=ids.domain_edit_id(d.name),
                                    size="xs",
                                    variant="subtle",
                                    leftSection=icon("tabler:pencil", 14),
                                ),
                            ],
                            gap=4,
                            wrap="nowrap",
                        )
                    ),
                ]
            )
        )
    return dmc.Table(
        [
            dmc.TableThead(
                dmc.TableTr(
                    [
                        dmc.TableTh("Domain"),
                        dmc.TableTh("Description"),
                        dmc.TableTh("Owner"),
                        dmc.TableTh("Functions"),
                        dmc.TableTh(""),
                    ]
                )
            ),
            dmc.TableTbody(rows),
        ],
        striped=True,
        highlightOnHover=True,
        withTableBorder=True,
    )


def register(app) -> None:
    @app.callback(
        Output(ids.DOMAINS_TABLE, "children", allow_duplicate=True),
        Input(ids.DOMAINS_FILTER, "value"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def filter_domains(text, persona):
        return domains_table(get_context(persona), text)

    @app.callback(
        Output(ids.DOMAIN_EDITING, "data"),
        Output(ids.DOMAIN_FORM_TITLE, "children"),
        Output(ids.DOMAIN_NAME, "value"),
        Output(ids.DOMAIN_NAME, "disabled"),
        Output(ids.DOMAIN_DISPLAY, "value"),
        Output(ids.DOMAIN_DESC, "value"),
        Output(ids.DOMAIN_OWNER, "value"),
        Output(ids.DOMAIN_SUBMIT, "children"),
        Output(ids.DOMAIN_DELETE_ZONE, "style"),
        Output(ids.DOMAIN_DELETE_CONFIRM, "value"),
        Output(ids.DOMAIN_RESULT, "children", allow_duplicate=True),
        Input({"type": "domain-edit", "name": ALL}, "n_clicks"),
        Input(ids.DOMAIN_CANCEL, "n_clicks"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def select_domain(n_edits, _cancel, persona):
        trigger = ctx.triggered_id
        c = get_context(persona)
        blank = (
            None,
            CREATE_TITLE,
            "",
            False,
            "",
            "",
            c.user.email or c.user.username,
            "Create domain",
            {"display": "none"},
            "",
            None,
        )
        if trigger == ids.DOMAIN_CANCEL or not isinstance(trigger, dict) or not any(n_edits or []):
            return blank
        try:
            d = c.backend.get_domain(trigger["name"])
        except BackendError as exc:
            return (*blank[:-1], error_alert(exc))
        return (
            d.name,
            f"Edit domain: {d.title}",
            d.name,
            True,
            d.display_name,
            d.description,
            d.owner,
            "Save changes",
            {},
            "",
            None,
        )

    @app.callback(
        Output(ids.DOMAIN_RESULT, "children"),
        Output(ids.DOMAINS_TABLE, "children"),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.DOMAIN_SUBMIT, "n_clicks"),
        Input(ids.DOMAIN_DELETE_SUBMIT, "n_clicks"),
        State(ids.DOMAIN_EDITING, "data"),
        State(ids.DOMAIN_NAME, "value"),
        State(ids.DOMAIN_DISPLAY, "value"),
        State(ids.DOMAIN_DESC, "value"),
        State(ids.DOMAIN_OWNER, "value"),
        State(ids.DOMAIN_DELETE_CONFIRM, "value"),
        State(ids.DOMAINS_FILTER, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def submit_domain(
        n_save, n_delete, editing, raw_name, display, desc, owner, confirm, filter_text, nav_version, persona
    ):
        trigger = ctx.triggered_id
        if (trigger == ids.DOMAIN_SUBMIT and not n_save) or (
            trigger == ids.DOMAIN_DELETE_SUBMIT and not n_delete
        ):
            return no_update, no_update, no_update, no_update
        c = get_context(persona)
        try:
            if trigger == ids.DOMAIN_DELETE_SUBMIT:
                if not editing:
                    return (
                        no_update,
                        no_update,
                        no_update,
                        notify("Pick a domain to delete first", color="yellow"),
                    )
                if (confirm or "").strip() != editing:
                    return (
                        no_update,
                        no_update,
                        no_update,
                        notify("Type the domain name exactly to confirm", color="yellow"),
                    )
                c.forms.delete_domain(editing)
                message = f"Domain '{editing}' deleted"
            elif editing:
                d = c.backend.get_domain(editing)
                d.display_name, d.description, d.owner = (
                    (display or "").strip(),
                    (desc or "").strip(),
                    (owner or "").strip(),
                )
                c.forms.update_domain(d)
                message = f"Domain '{d.title}' saved"
            else:
                name = sanitize_identifier(raw_name or "", fallback="")
                if not name:
                    return (
                        error_alert("A name is required.", "Cannot create"),
                        no_update,
                        no_update,
                        no_update,
                    )
                d = c.forms.create_domain(
                    DomainDef(name, (display or "").strip(), (desc or "").strip(), (owner or "").strip())
                )
                message = f"Domain '{d.title}' created"
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc, "Not applied"), no_update, no_update, no_update
        invalidate_metadata()
        return None, domains_table(get_context(persona), filter_text), (nav_version or 0) + 1, notify(message)
