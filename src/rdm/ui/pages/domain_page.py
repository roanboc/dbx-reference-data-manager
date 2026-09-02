"""Domain overview and administration (metadata, access grants), plus 'new domain'."""

from __future__ import annotations

import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, ctx, html, no_update

from rdm.backend.base import BackendError, NotFoundError
from rdm.models import DomainDef, Role, sanitize_identifier
from rdm.ui import ids
from rdm.ui.components import (
    ROLE_COLORS,
    empty_state,
    error_alert,
    icon,
    info_alert,
    link_button,
    notify,
    page_title,
    role_badge,
)
from rdm.ui.context import AppContext, get_context, invalidate_metadata
from rdm.ui.layout import domain_href, form_href

GRANTABLE = [Role.VIEWER, Role.EDITOR, Role.ADMIN]


def render(ctx_: AppContext, domain_name: str) -> dmc.Stack:
    role = ctx_.role_of(domain_name)
    if not role.can_view:
        return dmc.Stack([error_alert("You do not have access to this domain.", "Access denied")])
    try:
        domain = ctx_.backend.get_domain(domain_name)
    except NotFoundError as exc:
        return dmc.Stack([error_alert(str(exc), "Not found")])
    forms = ctx_.backend.list_forms(domain.name)
    header = page_title(
        domain.title,
        domain.description or None,
        crumbs=[dmc.Anchor("Home", href="/"), dmc.Text("Domain")],
        right=dmc.Group(
            [
                role_badge(role),
                dmc.Text(f"owner {domain.owner}" if domain.owner else "", size="sm", c="dimmed"),
            ],
            gap="sm",
        ),
    )
    cards = [
        dmc.Card(
            dmc.Stack(
                [
                    dmc.Group([icon("tabler:table", 18), dmc.Text(f.title, fw=600)], gap=6),
                    dmc.Text(f.description or "No description", size="sm", c="dimmed", lineClamp=2),
                    dmc.Text(
                        " · ".join(
                            x
                            for x in [
                                f"~{f.row_count:,} rows" if f.row_count is not None else "",
                                f"owner {f.owner}" if f.owner else "",
                            ]
                            if x
                        ),
                        size="xs",
                        c="dimmed",
                    ),
                    link_button(
                        "Open",
                        form_href(domain.name, f.name),
                        size="xs",
                        variant="light",
                        leftSection=icon("tabler:external-link", 14),
                    ),
                ],
                gap="xs",
            ),
            withBorder=True,
            radius="md",
            padding="md",
        )
        for f in forms
    ]
    blocks = [
        header,
        dmc.Title(f"Forms ({len(forms)})", order=4),
        dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3})
        if cards
        else empty_state(
            "No forms in this domain", "Administrators can create one from an Excel file with New form."
        ),
    ]
    if role.can_admin:
        blocks += [dmc.Divider(my="md"), _admin(ctx_, domain)]
    return dmc.Stack(blocks, gap="md")


def _admin(ctx_: AppContext, domain: DomainDef) -> dmc.Stack:
    settings = ctx_.settings
    form_block = dmc.Paper(
        dmc.Stack(
            [
                dmc.Title("Domain settings", order=4),
                dmc.TextInput(
                    id=ids.DOMAIN_DISPLAY, label="Display name", value=domain.display_name or domain.title
                ),
                dmc.Textarea(
                    id=ids.DOMAIN_DESC,
                    label="Description",
                    value=domain.description,
                    description="Stored as the schema comment",
                    autosize=True,
                    minRows=2,
                ),
                dmc.TextInput(id=ids.DOMAIN_OWNER, label="Owner", value=domain.owner),
                dmc.Group([dmc.Button("Save", id=ids.DOMAIN_SAVE, leftSection=icon("tabler:device-floppy"))]),
                html.Div(id=ids.DOMAIN_RESULT),
                dcc_store_domain(domain.name),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )
    grants_note = (
        "In Databricks these are Unity Catalog schema privileges managed in the asset bundle (resources/schemas.yml); this view reflects them."
        if settings.is_databricks
        else "Locally, roles are stored in the _rdm_meta.grants table and emulate Unity Catalog schema privileges."
    )
    grants_block = dmc.Paper(
        dmc.Stack(
            [
                dmc.Title("Access", order=4),
                dmc.Text(grants_note, size="sm", c="dimmed"),
                html.Div(id=ids.GRANTS_TABLE, children=grants_table(ctx_, domain.name)),
                dmc.Group(
                    [
                        dmc.TextInput(
                            id=ids.GRANT_PRINCIPAL,
                            placeholder="group or user, e.g. finance_stewards",
                            w=280,
                            leftSection=icon("tabler:users"),
                        ),
                        dmc.Select(
                            id=ids.GRANT_ROLE,
                            data=[{"value": r.name, "label": r.label} for r in GRANTABLE],
                            value=Role.VIEWER.name,
                            w=180,
                            allowDeselect=False,
                        ),
                        dmc.Button(
                            "Grant",
                            id=ids.GRANT_SUBMIT,
                            leftSection=icon("tabler:user-plus"),
                            variant="light",
                        ),
                    ],
                    align="flex-end",
                    style={"display": "none"} if settings.is_databricks else {},
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )
    return dmc.Stack([form_block, grants_block], gap="md")


def dcc_store_domain(name: str):
    from dash import dcc

    return dcc.Store(id=ids.DOMAIN_KEY, data=name)


def grants_table(ctx_: AppContext, domain: str) -> dmc.Table | dmc.Text:
    try:
        grants = ctx_.forms.list_domain_grants(domain)
    except BackendError as exc:
        return error_alert(exc)
    if not grants:
        return dmc.Text("No explicit grants on this domain.", size="sm", c="dimmed")
    rows = []
    for principal, role in grants:
        rows.append(
            dmc.TableTr(
                [
                    dmc.TableTd(
                        dmc.Group([icon("tabler:users-group", 16), dmc.Text(principal, size="sm")], gap=6)
                    ),
                    dmc.TableTd(dmc.Badge(role.label, color=ROLE_COLORS[role], variant="light", size="sm")),
                    dmc.TableTd(
                        dmc.Button(
                            "Revoke",
                            id=ids.revoke_id(principal),
                            size="xs",
                            variant="subtle",
                            color="red",
                            disabled=ctx_.settings.is_databricks,
                            leftSection=icon("tabler:user-minus", 14),
                        )
                    ),
                ]
            )
        )
    return dmc.Table(
        [
            dmc.TableThead(dmc.TableTr([dmc.TableTh("Principal"), dmc.TableTh("Role"), dmc.TableTh("")])),
            dmc.TableTbody(rows),
        ],
        striped=True,
        highlightOnHover=True,
        withTableBorder=True,
    )


def render_new(ctx_: AppContext) -> dmc.Stack:
    header = page_title(
        "New domain", "A domain is a Unity Catalog schema that groups the forms of one business function."
    )
    if not ctx_.permissions.can_create_domain:
        msg = (
            "In Databricks, domains are created through the asset bundle (resources/schemas.yml)."
            if ctx_.settings.is_databricks
            else "Creating domains requires catalog administrator rights."
        )
        return dmc.Stack([header, info_alert(msg, "Managed as infrastructure", "yellow")])
    return dmc.Stack(
        [
            header,
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.TextInput(
                            id=ids.NEW_DOMAIN_NAME,
                            label="Name",
                            placeholder="e.g. student__survey_service_improvement",
                            description="Convention: <business_function>__<area>; becomes the schema name",
                            required=True,
                        ),
                        dmc.TextInput(
                            id=ids.NEW_DOMAIN_DISPLAY,
                            label="Display name",
                            placeholder="Student Survey & Service Improvement",
                        ),
                        dmc.Textarea(id=ids.NEW_DOMAIN_DESC, label="Description", autosize=True, minRows=2),
                        dmc.TextInput(
                            id=ids.NEW_DOMAIN_OWNER,
                            label="Owner",
                            value=ctx_.user.email or ctx_.user.username,
                        ),
                        dmc.Group(
                            [
                                dmc.Button(
                                    "Create domain",
                                    id=ids.NEW_DOMAIN_SUBMIT,
                                    leftSection=icon("tabler:folder-plus"),
                                )
                            ]
                        ),
                        html.Div(id=ids.NEW_DOMAIN_RESULT),
                    ],
                    gap="sm",
                ),
                withBorder=True,
                p="md",
                radius="md",
                maw=720,
            ),
        ]
    )


def register(app) -> None:
    @app.callback(
        Output(ids.DOMAIN_RESULT, "children"),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.DOMAIN_SAVE, "n_clicks"),
        State(ids.DOMAIN_KEY, "data"),
        State(ids.DOMAIN_DISPLAY, "value"),
        State(ids.DOMAIN_DESC, "value"),
        State(ids.DOMAIN_OWNER, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def save_domain(n, name, display, desc, owner, nav_version, persona):
        if not n:
            return no_update, no_update, no_update
        c = get_context(persona)
        try:
            domain = c.backend.get_domain(name)
            domain.display_name, domain.description, domain.owner = (
                (display or "").strip(),
                (desc or "").strip(),
                (owner or "").strip(),
            )
            c.forms.update_domain(domain)
        except (BackendError, ValueError) as exc:
            return error_alert(exc), no_update, no_update
        invalidate_metadata()
        return None, (nav_version or 0) + 1, notify("Domain saved")

    @app.callback(
        Output(ids.GRANTS_TABLE, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.GRANT_SUBMIT, "n_clicks"),
        Input({"type": "revoke", "principal": ALL}, "n_clicks"),
        State(ids.DOMAIN_KEY, "data"),
        State(ids.GRANT_PRINCIPAL, "value"),
        State(ids.GRANT_ROLE, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def change_grants(n_grant, n_revokes, domain, principal, role_name, nav_version, persona):
        trigger = ctx.triggered_id
        if trigger is None or (not any(n_revokes or []) and not n_grant):
            return no_update, no_update, no_update
        c = get_context(persona)
        try:
            if trigger == ids.GRANT_SUBMIT:
                if not (principal or "").strip():
                    return no_update, notify("Enter a group or user name", color="red"), no_update
                c.forms.grant_domain_role(domain, principal.strip(), Role[role_name])
                message = f"Granted {Role[role_name].label} to {principal.strip()}"
            else:
                c.forms.grant_domain_role(domain, trigger["principal"], Role.NONE)
                message = f"Revoked access of {trigger['principal']}"
        except (BackendError, ValueError, KeyError) as exc:
            return no_update, notify(str(exc), title="Not applied", color="red"), no_update
        invalidate_metadata()
        return grants_table(get_context(persona), domain), notify(message), (nav_version or 0) + 1

    @app.callback(
        Output(ids.NEW_DOMAIN_RESULT, "children"),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.NEW_DOMAIN_SUBMIT, "n_clicks"),
        State(ids.NEW_DOMAIN_NAME, "value"),
        State(ids.NEW_DOMAIN_DISPLAY, "value"),
        State(ids.NEW_DOMAIN_DESC, "value"),
        State(ids.NEW_DOMAIN_OWNER, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def create_domain(n, raw_name, display, desc, owner, nav_version, persona):
        if not n:
            return no_update, no_update, no_update, no_update
        name = sanitize_identifier(raw_name or "", fallback="")
        if not name:
            return error_alert("A name is required.", "Cannot create"), no_update, no_update, no_update
        c = get_context(persona)
        try:
            domain = c.forms.create_domain(
                DomainDef(name, (display or "").strip(), (desc or "").strip(), (owner or "").strip())
            )
        except (BackendError, ValueError) as exc:
            return error_alert(exc, "Cannot create"), no_update, no_update, no_update
        invalidate_metadata()
        return (
            None,
            domain_href(domain.name),
            (nav_version or 0) + 1,
            notify(f"Domain '{domain.title}' created"),
        )
