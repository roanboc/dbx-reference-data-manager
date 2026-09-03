"""Small presentational helpers shared by the pages."""

from __future__ import annotations

import uuid
from typing import Any

import dash_mantine_components as dmc
from dash import dcc, html

from rdm.backend.base import BackendError
from rdm.models import GLOBAL_ADMIN_LABEL, DataType, FormDef, Role, ValidationIssue

ROLE_COLORS = {Role.ADMIN: "grape", Role.EDITOR: "teal", Role.VIEWER: "blue", Role.NONE: "gray"}
ROLE_ICONS = {
    Role.ADMIN: "tabler:shield-check",
    Role.EDITOR: "tabler:pencil",
    Role.VIEWER: "tabler:eye",
    Role.NONE: "tabler:ban",
}
TYPE_ICONS = {
    DataType.STRING: "tabler:letter-case",
    DataType.INTEGER: "tabler:hash",
    DataType.DECIMAL: "tabler:decimal",
    DataType.DOUBLE: "tabler:math-function",
    DataType.BOOLEAN: "tabler:checkbox",
    DataType.DATE: "tabler:calendar",
    DataType.TIMESTAMP: "tabler:clock",
    DataType.OTHER: "tabler:code",
}


def icon(name: str, size: int = 16, color: str | None = None) -> html.Span:
    """A Tabler icon (``tabler:<name>``) rendered from the vendored SVG in ``assets/icons/``.

    Icons are painted with ``currentColor`` through a CSS mask (``assets/styles.css``), so they
    follow the surrounding text and the colour scheme; ``color`` names a Mantine colour.
    Add new names with ``scripts/vendor_icons.py``; ``tests/test_ui_icons.py`` checks the set.
    """
    style: dict[str, Any] = {"width": f"{size}px", "height": f"{size}px"}
    if color:
        style["color"] = f"var(--mantine-color-{color}-filled)"
    return html.Span(
        className=f"rdm-icon rdm-icon-{name.removeprefix('tabler:')}", style=style, **{"aria-hidden": "true"}
    )


def role_badge(role: Role, size: str = "sm") -> dmc.Badge:
    return dmc.Badge(
        role.label,
        color=ROLE_COLORS[role],
        variant="light",
        size=size,
        leftSection=icon(ROLE_ICONS[role], 12),
    )


def domain_badge(title: str, size: str = "sm") -> dmc.Badge:
    """The domain a function belongs to (the top of the domain > function > form hierarchy)."""
    return dmc.Badge(
        title, color="indigo", variant="outline", size=size, leftSection=icon("tabler:sitemap", 12)
    )


def copy_code(value: str, label: str | None = None, size: str = "sm") -> dmc.Group:
    """A code chip with a copy-to-clipboard button (for Databricks paths and query snippets)."""
    return dmc.Group(
        [
            dmc.Text(label, size="xs", c="dimmed") if label else None,
            dmc.Code(
                value,
                style={
                    "fontSize": "12px" if size == "sm" else "13px",
                    "whiteSpace": "pre-wrap",
                    "wordBreak": "break-all",
                    "minWidth": 0,
                },
            ),
            dmc.CopyButton(
                dmc.Group([icon("tabler:copy", 14), dmc.Text("Copy", size="xs")], gap=4, wrap="nowrap"),
                copiedChildren=dmc.Group(
                    [icon("tabler:check", 14), dmc.Text("Copied", size="xs")], gap=4, wrap="nowrap"
                ),
                value=value,
                timeout=1500,
                variant="subtle",
                size="compact-xs",
                color="gray",
                copiedColor="teal",
                style={"flex": "0 0 auto"},
                **{"aria-label": "Copy to clipboard"},
            ),
        ],
        gap=6,
        wrap="nowrap",
        align="center",
        style={"minWidth": 0, "maxWidth": "100%"},
    )


def databricks_path_block(title: str, rows: list[tuple[str, str]], note: str) -> dmc.Paper:
    """Settings block listing copy-ready paths and query snippets."""
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Title(title, order=4),
                dmc.Text(note, size="sm", c="dimmed"),
                dmc.Stack(
                    [
                        dmc.Stack([dmc.Text(label, size="xs", fw=600), copy_code(value, size="md")], gap=2)
                        for label, value in rows
                    ],
                    gap="sm",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )


def global_admin_badge(size: str = "sm") -> dmc.Badge:
    return dmc.Badge(
        GLOBAL_ADMIN_LABEL,
        color="orange",
        variant="light",
        size=size,
        leftSection=icon("tabler:world-cog", 12),
    )


def notify(
    message: str,
    title: str | None = None,
    color: str = "teal",
    icon_name: str | None = None,
    auto_close: int | bool = 4000,
) -> list[dict]:
    """Payload for ``NotificationContainer.sendNotifications``."""
    return [
        {
            "id": f"n-{uuid.uuid4().hex}",
            "action": "show",
            "title": title,
            "message": message,
            "color": color,
            "autoClose": auto_close,
            "icon": icon(icon_name or ("tabler:check" if color == "teal" else "tabler:alert-circle")),
        }
    ]


def error_alert(exc: Exception | str, title: str = "Something went wrong") -> dmc.Alert:
    message = str(exc) if isinstance(exc, BackendError | ValueError | str) else f"Unexpected error: {exc}"
    return dmc.Alert(message, title=title, color="red", icon=icon("tabler:alert-circle"), variant="light")


def info_alert(message: str, title: str | None = None, color: str = "blue") -> dmc.Alert:
    return dmc.Alert(message, title=title, color=color, icon=icon("tabler:info-circle"), variant="light")


def issues_list(issues: list[ValidationIssue], limit: int = 12) -> html.Div:
    items = [dmc.ListItem(str(i)) for i in issues[:limit]]
    if len(issues) > limit:
        items.append(dmc.ListItem(f"... and {len(issues) - limit} more"))
    return html.Div(dmc.List(items, size="sm", spacing=2, c="red"))


def empty_state(title: str, body: str, icon_name: str = "tabler:inbox") -> dmc.Paper:
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.ThemeIcon(icon(icon_name, 22), size="xl", radius="xl", variant="light", color="gray"),
                dmc.Text(title, fw=600),
                dmc.Text(body, size="sm", c="dimmed", ta="center"),
            ],
            align="center",
            gap="xs",
        ),
        withBorder=True,
        p="xl",
        radius="md",
    )


def form_meta(form: FormDef) -> str:
    bits = []
    if form.row_count is not None:
        bits.append(f"{form.row_count:,} rows")
    if form.owner:
        bits.append(f"owner {form.owner}")
    if form.updated_at is not None:
        who = f" by {form.updated_by}" if form.updated_by else ""
        bits.append(f"updated {form.updated_at:%Y-%m-%d %H:%M}{who}")
    return " · ".join(bits)


def page_title(
    title: str, subtitle: str | None = None, crumbs: list[Any] | None = None, right: Any = None
) -> dmc.Group:
    left = dmc.Stack(
        [
            dmc.Breadcrumbs(crumbs, separator="›") if crumbs else None,
            dmc.Title(title, order=2),
            dmc.Text(subtitle, c="dimmed", size="sm") if subtitle else None,
        ],
        gap=2,
    )
    return dmc.Group(
        [left, right] if right is not None else [left], justify="space-between", align="flex-start", mb="md"
    )


def link_button(label: str, href: str, **button_props: Any) -> dcc.Link:
    """A Mantine button that navigates client-side (DMC buttons do not take ``href``)."""
    return dcc.Link(
        dmc.Button(label, **button_props), href=href, refresh=False, style={"textDecoration": "none"}
    )
