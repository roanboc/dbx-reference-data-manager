"""Small presentational helpers shared by the pages."""

from __future__ import annotations

import uuid
from typing import Any

import dash_mantine_components as dmc
from dash import dcc, html
from dash_iconify import DashIconify

from rdm.backend.base import BackendError
from rdm.models import DataType, FormDef, Role, ValidationIssue

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


def icon(name: str, size: int = 16, **kwargs: Any) -> DashIconify:
    return DashIconify(icon=name, width=size, height=size, **kwargs)


def role_badge(role: Role, size: str = "sm") -> dmc.Badge:
    return dmc.Badge(
        role.label,
        color=ROLE_COLORS[role],
        variant="light",
        size=size,
        leftSection=icon(ROLE_ICONS[role], 12),
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
