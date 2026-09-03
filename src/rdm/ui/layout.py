"""Application shell: header, navigation sidebar and the routed page container."""

from __future__ import annotations

from urllib.parse import quote

import dash_mantine_components as dmc
from dash import dcc, html

from rdm.config import APP_TITLE
from rdm.services import NavDomain
from rdm.ui import ids
from rdm.ui.components import ROLE_COLORS, ROLE_ICONS, global_admin_badge, icon, link_button
from rdm.ui.context import AppContext

THEME = {
    "primaryColor": "indigo",
    "fontFamily": "Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    "defaultRadius": "md",
    "headings": {"fontWeight": "650"},
}


def form_href(function: str, form: str) -> str:
    return f"/f/{quote(function)}/{quote(form)}"


def function_href(function: str) -> str:
    return f"/fn/{quote(function)}"


def file_href(function: str, name: str) -> str:
    return f"/file/{quote(function)}/{quote(name)}"


def domain_href(domain: str) -> str:
    return f"/dm/{quote(domain)}"


DOMAINS_HREF = "/domains"
NEW_FUNCTION_HREF = "/new-function"
NEW_FORM_HREF = "/new-form"


def shell() -> dmc.MantineProvider:
    return dmc.MantineProvider(
        theme=THEME,
        children=[
            dcc.Location(id=ids.URL, refresh=False),
            dcc.Store(id=ids.PERSONA, storage_type="session"),
            dcc.Store(id=ids.NAV_VERSION, data=0),
            dcc.Download(id=ids.DOWNLOAD),
            dmc.NotificationContainer(id=ids.NOTIFY, position="top-right"),
            dmc.AppShell(
                [
                    dmc.AppShellHeader(html.Div(id="header-content"), px="md"),
                    dmc.AppShellNavbar(
                        id="navbar",
                        children=dmc.Stack(
                            [
                                dmc.TextInput(
                                    id=ids.NAV_SEARCH,
                                    placeholder="Search forms and files",
                                    leftSection=icon("tabler:search"),
                                    debounce=350,
                                    size="sm",
                                ),
                                html.Div(
                                    id=ids.NAVBAR,
                                    style={
                                        "flex": 1,
                                        "minHeight": 0,
                                        "display": "flex",
                                        "flexDirection": "column",
                                    },
                                ),
                            ],
                            gap="sm",
                            h="100%",
                        ),
                        p="sm",
                    ),
                    dmc.AppShellMain(
                        dmc.Container(html.Div(id=ids.PAGE), size="xl", px="md", py="md", fluid=True)
                    ),
                ],
                header={"height": 56},
                navbar={"width": 330, "breakpoint": "sm", "collapsed": {"mobile": True}},
                padding="md",
            ),
        ],
    )


def header(ctx: AppContext, persona: str | None) -> dmc.Group:
    left = dmc.Group(
        [
            dmc.ThemeIcon(icon("tabler:table-options", 20), size="lg", radius="md", variant="light"),
            dmc.Title(APP_TITLE, order=4),
            dmc.Badge(ctx.backend.describe(), variant="outline", color="gray", size="sm"),
        ],
        gap="sm",
    )
    help_button = link_button(
        "Help", "/help", variant="subtle", size="sm", leftSection=icon("tabler:help-circle")
    )
    admin_badge = global_admin_badge() if ctx.permissions.is_global_admin else None
    if ctx.auth.supports_persona_switching:
        personas = ctx.auth.personas()
        current = persona if persona in {p.key for p in personas} else ctx.settings.persona
        right = dmc.Group(
            [
                help_button,
                admin_badge,
                dmc.Text("Local persona", size="sm", c="dimmed"),
                dmc.Select(
                    id=ids.PERSONA_SELECT,
                    data=[{"value": p.key, "label": p.label} for p in personas],
                    value=current,
                    size="sm",
                    w=220,
                    allowDeselect=False,
                    leftSection=icon("tabler:user-circle"),
                ),
            ],
            gap="xs",
        )
    else:
        mode = "queries run as you" if ctx.auth.access_token() else "queries run as the app service principal"
        right = dmc.Group(
            [
                help_button,
                admin_badge,
                icon("tabler:user-circle", 20),
                dmc.Stack(
                    [dmc.Text(ctx.user.label, size="sm", fw=500), dmc.Text(mode, size="xs", c="dimmed")],
                    gap=0,
                ),
            ],
            gap="xs",
        )
    return dmc.Group([left, right], justify="space-between", h=56)


def navbar(ctx: AppContext, groups: list[NavDomain], pathname: str, search: str | None) -> dmc.Stack:
    """Sidebar: functions grouped under their domain, forms under each function."""
    perms = ctx.permissions
    persona = ctx.auth.personas()
    caption = next((p.description for p in persona if p.user.username == ctx.user.username), "")
    blocks = []
    if caption:
        blocks.append(dmc.Text(caption, size="xs", c="dimmed"))
    if not groups:
        blocks.append(
            dmc.Alert(
                "No matches. Try another word."
                if search
                else "You have not been granted access to any function yet.",
                color="gray",
                variant="light",
                icon=icon("tabler:search-off" if search else "tabler:lock"),
            )
        )
    current_function = _current_function(pathname)
    sections = []
    for group in groups:
        accordion_items = []
        opened = []
        for item in group.functions:
            f = item.function
            if search or f.name == current_function:
                opened.append(f.name)
            links = [
                dmc.NavLink(
                    label="Function overview",
                    href=function_href(f.name),
                    active=pathname == function_href(f.name),
                    leftSection=icon("tabler:folder-open"),
                    variant="light",
                )
            ]
            for x in item.forms:
                links.append(
                    dmc.NavLink(
                        label=x.title,
                        description=(x.description[:80] + "…")
                        if x.description and len(x.description) > 80
                        else (x.description or None),
                        href=form_href(f.name, x.name),
                        active=pathname == form_href(f.name, x.name),
                        leftSection=icon("tabler:table"),
                        variant="light",
                    )
                )
            for x in item.files:
                links.append(
                    dmc.NavLink(
                        label=x.title,
                        description=(x.description[:80] + "…")
                        if x.description and len(x.description) > 80
                        else (x.description or f"{x.format.upper()} file"),
                        href=file_href(f.name, x.name),
                        active=pathname == file_href(f.name, x.name),
                        leftSection=icon(
                            "tabler:file-spreadsheet" if x.format == "csv" else "tabler:file-database"
                        ),
                        variant="light",
                    )
                )
            if item.is_empty:
                links.append(
                    dmc.Text(
                        "No forms or files yet."
                        + (" Use New form or Add file to create one." if item.role.can_admin else ""),
                        size="xs",
                        c="dimmed",
                        px="sm",
                    )
                )
            accordion_items.append(
                dmc.AccordionItem(
                    [
                        dmc.AccordionControl(
                            dmc.Group(
                                [
                                    dmc.Text(f.title, size="sm", fw=600),
                                    dmc.Badge(
                                        item.role.label,
                                        size="xs",
                                        color=ROLE_COLORS[item.role],
                                        variant="light",
                                    ),
                                ],
                                justify="space-between",
                                wrap="nowrap",
                            ),
                            icon=icon(ROLE_ICONS[item.role]),
                        ),
                        dmc.AccordionPanel(dmc.Stack(links, gap=0)),
                    ],
                    value=f.name,
                )
            )
        sections.append(
            dmc.Stack(
                [
                    dmc.Anchor(
                        _domain_heading(group),
                        href=domain_href(group.domain.name),
                        underline="never",
                        c="inherit",
                    )
                    if not group.is_unassigned
                    else _domain_heading(group),
                    dmc.Accordion(
                        accordion_items,
                        multiple=True,
                        value=opened,
                        variant="separated",
                        chevronPosition="left",
                    ),
                ],
                gap=4,
                className="rdm-domain-group",
            )
        )
    if sections:
        blocks.append(dmc.ScrollArea(dmc.Stack(sections, gap="sm"), type="auto", style={"flex": 1}))
    actions = []
    if perms.is_admin_anywhere:
        actions.append(
            link_button(
                "New form",
                NEW_FORM_HREF,
                leftSection=icon("tabler:circle-plus"),
                variant="light",
                fullWidth=True,
                disabled=not perms.admin_functions,
            )
        )
    if perms.can_create_function:
        actions.append(
            link_button(
                "New function",
                NEW_FUNCTION_HREF,
                leftSection=icon("tabler:folder-plus"),
                variant="light",
                fullWidth=True,
            )
        )
    if perms.can_manage_domains:
        actions.append(
            link_button(
                "Domains",
                DOMAINS_HREF,
                leftSection=icon("tabler:sitemap"),
                variant="light",
                fullWidth=True,
            )
        )
    actions.append(
        link_button("Home", "/", leftSection=icon("tabler:home"), variant="subtle", fullWidth=True)
    )
    blocks.append(dmc.Divider())
    blocks.append(dmc.Stack(actions, gap="xs"))
    access = [dmc.Text(f"Your access: {perms.summary}", size="xs", c="dimmed")]
    if perms.is_global_admin:
        access.append(global_admin_badge(size="xs"))
    blocks.append(dmc.Group(access, gap=6))
    return dmc.Stack(blocks, gap="sm", h="100%")


def _domain_heading(group: NavDomain) -> dmc.Group:
    return dmc.Group(
        [
            icon("tabler:sitemap" if not group.is_unassigned else "tabler:folder-question", 14),
            dmc.Text(group.domain.title, size="xs", fw=700, tt="uppercase", c="dimmed"),
        ],
        gap=6,
        px=4,
    )


def _current_function(pathname: str) -> str | None:
    parts = [p for p in (pathname or "").split("/") if p]
    if len(parts) >= 2 and parts[0] in ("fn", "f", "file"):
        return parts[1]
    return None
