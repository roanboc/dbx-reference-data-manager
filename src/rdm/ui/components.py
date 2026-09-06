"""Small presentational helpers shared by the pages."""

from __future__ import annotations

import uuid
from typing import Any

import dash_mantine_components as dmc
import pandas as pd
from dash import dcc, html

from rdm.backend.base import BackendError
from rdm.models import GLOBAL_ADMIN_LABEL, DataType, FileDef, FormDef, Role, ValidationIssue, split_file_name
from rdm.services import NavFunction
from rdm.services.files import human_size, preview_bytes
from rdm.ui import grid as g
from rdm.ui.routes import file_href, form_href, function_href

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


def meta_line(obj: FormDef | FileDef) -> str:
    """Size (files), row count, owner and last change of a form or file, for page headers."""
    bits = [human_size(obj.size_bytes)] if isinstance(obj, FileDef) else []
    if obj.row_count is not None:
        bits.append(f"{obj.row_count:,} rows")
    if obj.owner:
        bits.append(f"owner {obj.owner}")
    if obj.updated_at is not None:
        who = f" by {obj.updated_by}" if obj.updated_by else ""
        bits.append(f"updated {obj.updated_at:%Y-%m-%d %H:%M}{who}")
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


def stat_tile(label: str, value: int, icon_name: str) -> dmc.Paper:
    """One number with a label and an icon (home and domain pages)."""
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


def danger_zone(
    text: str,
    confirm_id: str,
    submit_id: str,
    button_label: str,
    confirm_placeholder: str,
    disabled: bool = False,
) -> dmc.Paper:
    """The typed-confirmation delete block every object page shows to global admins."""
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Title("Danger zone", order=4, c="red"),
                dmc.Text(text, size="sm"),
                dmc.Group(
                    [
                        dmc.TextInput(id=confirm_id, placeholder=confirm_placeholder, w=340),
                        dmc.Button(
                            button_label,
                            id=submit_id,
                            color="red",
                            disabled=disabled,
                            leftSection=icon("tabler:trash-x"),
                        ),
                    ],
                    align="flex-end",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
        style={"borderColor": "var(--mantine-color-red-filled)"},
    )


def dropzone(upload_id: str, what: str, accept: str, max_size: int | None = None) -> dcc.Upload:
    """The file drop area used by every upload dialog (``what`` completes "click to choose ...")."""
    props: dict[str, Any] = {"max_size": max_size} if max_size else {}
    return dcc.Upload(
        id=upload_id,
        children=html.Div(["Drag and drop or ", html.B("click to choose"), f" {what}"]),
        className="rdm-dropzone",
        multiple=False,
        accept=accept,
        **props,
    )


def upload_preview(name: str, data: bytes) -> dmc.Stack:
    """Size, format and the first rows of an uploaded CSV/Parquet file (parsed locally, before storing)."""
    fmt = split_file_name(name)[1]
    frame = preview_bytes(data, fmt)
    return dmc.Stack(
        [
            dmc.Text(
                f"{name} · {fmt.upper()} · {human_size(len(data))} · {len(frame.columns)} columns "
                f"(showing the first {len(frame)} rows)",
                size="sm",
                fw=500,
            ),
            g.preview_grid(g.records_from_frame(frame), g.frame_column_defs(frame), "240px"),
        ],
        gap="xs",
    )


def fmt_cell(value: Any) -> str:
    """A history or review cell as text: missing values are blank, timestamps are compact."""
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def file_icon(fmt: str) -> str:
    return "tabler:file-spreadsheet" if fmt == "csv" else "tabler:file-database"


def matching_objects(item: NavFunction, needle: str) -> tuple[list[FormDef], list[FileDef]] | None:
    """The forms and files of a function that match a filter text (all of them when the function
    itself matches); ``None`` when nothing matches. Names, titles, descriptions and owners count."""
    f = item.function
    if not needle or needle in f"{f.name} {f.title} {f.description} {f.owner}".lower():
        return item.forms, item.files
    forms = [x for x in item.forms if needle in f"{x.name} {x.title} {x.description} {x.owner}".lower()]
    files = [x for x in item.files if needle in f"{x.name} {x.title} {x.description} {x.owner}".lower()]
    return (forms, files) if forms or files else None


def object_links(function: str, forms: list[FormDef], files: list[FileDef]) -> list[Any]:
    """Links to the forms and files of a function, or a note when it has none."""
    links: list[Any] = [
        dmc.Anchor(
            dmc.Group([icon("tabler:table", 14), dmc.Text(x.title, size="sm")], gap=6),
            href=form_href(function, x.name),
            underline="never",
        )
        for x in forms
    ]
    links += [
        dmc.Anchor(
            dmc.Group(
                [
                    icon(file_icon(x.format), 14),
                    dmc.Text(x.title, size="sm"),
                    dmc.Badge(x.format.upper(), size="xs", variant="outline", color="gray"),
                ],
                gap=6,
            ),
            href=file_href(function, x.name),
            underline="never",
        )
        for x in files
    ]
    return links or [dmc.Text("No forms or files yet.", size="sm", c="dimmed")]


def function_card(
    item: NavFunction, forms: list[FormDef], files: list[FileDef], meta: str, details: list[Any] | None = None
) -> dmc.Card:
    """A function with its role badge, description, links to its objects and the actions to open it."""
    f = item.function
    actions: list[Any] = [
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
                    "Documentation", variant="subtle", size="xs", leftSection=icon("tabler:external-link", 14)
                ),
                href=f.doc_link,
                target="_blank",
            )
        )
    return dmc.Card(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Group([icon("tabler:folder", 18), dmc.Text(f.title, fw=600)], gap=6),
                        role_badge(item.role),
                    ],
                    justify="space-between",
                ),
                *(details or []),
                dmc.Text(f.description or "No description", size="sm", c="dimmed"),
                dmc.Text(meta, size="xs", c="dimmed"),
                dmc.Stack(object_links(f.name, forms, files), gap=4),
                dmc.Group(actions, gap="xs"),
            ],
            gap="xs",
        ),
        withBorder=True,
        radius="md",
        padding="md",
    )
