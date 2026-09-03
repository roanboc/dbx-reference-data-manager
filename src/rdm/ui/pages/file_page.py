"""A file: a CSV / Parquet dataset in a function's volume. Preview, columns, history, replace, settings.

Files are the third leaf of the hierarchy (domain > function > form | file) for lists too
large to maintain in a grid. They follow the function's roles: viewers preview and download,
editors replace the content, function admins add files and edit their details, global admins
delete them.
"""

from __future__ import annotations

import logging
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Input, Output, State, dcc, html, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import FileDef, Role, qualified_name, volume_file_path
from rdm.services.files import FileError, check_upload, human_size, preview_bytes
from rdm.ui import grid as g
from rdm.ui import ids, uploads
from rdm.ui.components import (
    TYPE_ICONS,
    copy_code,
    databricks_path_block,
    empty_state,
    error_alert,
    icon,
    info_alert,
    notify,
    page_title,
    role_badge,
)
from rdm.ui.context import AppContext, get_context, invalidate_metadata
from rdm.ui.layout import function_href

log = logging.getLogger(__name__)
GRID_THEME = "ag-theme-quartz"
PREVIEW_LIMIT = 200
CHANGE_LABELS = {"upload": "Uploaded", "replace": "Replaced", "delete": "Deleted"}


def render(ctx_: AppContext, function: str, name: str) -> dmc.Stack:
    file = ctx_.forms.get_file(function, name)
    role = ctx_.role_of(function)
    try:
        function_title = ctx_.backend.get_function(function).title
    except BackendError:
        function_title = function
    dbx_path = volume_file_path(ctx_.settings.catalog, function, name)
    header = page_title(
        file.title,
        file.description or None,
        crumbs=[dmc.Anchor(function_title, href=function_href(function)), dmc.Text(file.name)],
        right=dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Badge(file.format.upper(), variant="outline", color="gray", size="sm"),
                        role_badge(role),
                    ],
                    justify="flex-end",
                    gap="xs",
                ),
                dmc.Text(_meta(file), size="xs", c="dimmed", ta="right"),
                dmc.Group([copy_code(dbx_path)], justify="flex-end"),
            ],
            gap=4,
            align="flex-end",
        ),
    )
    toolbar = [
        dmc.Button(
            "Download",
            id=ids.FILE_DOWNLOAD,
            variant="default",
            size="sm",
            leftSection=icon("tabler:download"),
        ),
        dmc.Button(
            "Replace file",
            id=ids.FILE_REPLACE_OPEN,
            variant="light",
            size="sm",
            leftSection=icon("tabler:upload"),
            style={} if role.can_edit else {"display": "none"},
        ),
    ]
    tabs = [
        dmc.TabsTab("Preview", value="preview", leftSection=icon("tabler:table")),
        dmc.TabsTab("Columns", value="columns", leftSection=icon("tabler:columns")),
        dmc.TabsTab("History", value="history", leftSection=icon("tabler:history")),
    ]
    panels = [
        dmc.TabsPanel(preview_panel(ctx_, file), value="preview", pt="sm"),
        dmc.TabsPanel(
            html.Div(id=ids.FILE_COLUMNS_PANEL, children=dmc.Loader(size="sm")), value="columns", pt="sm"
        ),
        dmc.TabsPanel(
            html.Div(id=ids.FILE_HISTORY_PANEL, children=dmc.Loader(size="sm")), value="history", pt="sm"
        ),
    ]
    if role.can_admin:
        tabs.append(dmc.TabsTab("Settings", value="settings", leftSection=icon("tabler:settings")))
        panels.append(
            dmc.TabsPanel(
                _settings_tab(file, ctx_.permissions.can_delete, dbx_path, ctx_.settings),
                value="settings",
                pt="sm",
            )
        )
    notes = []
    if not file.registered:
        notes.append(
            info_alert(
                "This file was found in the function's volume without a registry entry (landed by a pipeline or "
                "copied in). Function admins can give it a display name, description and owner under Settings.",
                color="yellow",
            )
        )
    return dmc.Stack(
        [
            dcc.Store(id=ids.FILE_KEY, data={"function": function, "name": name}),
            dcc.Store(id=ids.FILE_REPLACE_TOKEN, data=None),
            header,
            *notes,
            dmc.Group(toolbar, gap="xs"),
            dmc.Tabs([dmc.TabsList(tabs), *panels], value="preview", id=ids.FILE_TABS, keepMounted=True),
            _replace_modal(ctx_, file),
        ],
        gap="xs",
    )


def _meta(file: FileDef) -> str:
    bits = [human_size(file.size_bytes)]
    if file.row_count is not None:
        bits.append(f"{file.row_count:,} rows")
    if file.owner:
        bits.append(f"owner {file.owner}")
    if file.updated_at is not None:
        who = f" by {file.updated_by}" if file.updated_by else ""
        bits.append(f"updated {file.updated_at:%Y-%m-%d %H:%M}{who}")
    return " · ".join(bits)


def preview_panel(ctx_: AppContext, file: FileDef) -> Any:
    try:
        frame = ctx_.forms.preview_file(file, limit=PREVIEW_LIMIT)
    except BackendError as exc:
        return error_alert(exc, "Cannot preview the file")
    if frame.empty and not len(frame.columns):
        return empty_state("Empty file", "The file has no rows.", "tabler:file-off")
    total = f" of {file.row_count:,}" if file.row_count is not None else ""
    return dmc.Stack(
        [
            dmc.Text(
                f"First {len(frame):,}{total} rows, read from the stored file. Files are not edited in place: "
                "download, change and replace the file, or land a new version from a pipeline.",
                size="xs",
                c="dimmed",
            ),
            dag.AgGrid(
                id=ids.FILE_PREVIEW_GRID,
                rowData=g.records_from_frame(frame),
                columnDefs=g.frame_column_defs(frame),
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={"rowHeight": 32, "enableCellTextSelection": True, "ensureDomOrder": True},
                columnSize="autoSize",
                className=GRID_THEME,
                style={"height": "58vh", "width": "100%"},
            ),
        ],
        gap="xs",
    )


def columns_panel(ctx_: AppContext, file: FileDef) -> Any:
    try:
        columns = ctx_.forms.file_columns(file)
    except BackendError as exc:
        return error_alert(exc, "Cannot read the columns")
    rows = [
        dmc.TableTr(
            [
                dmc.TableTd(dmc.Group([icon(TYPE_ICONS[c.data_type], 14), dmc.Code(c.name)], gap=6)),
                dmc.TableTd(c.native_type or c.type_label),
                dmc.TableTd(c.data_type.label),
            ]
        )
        for c in columns
    ]
    return dmc.Stack(
        [
            dmc.Text(
                "Columns and types as the reader infers them from the file; there is nothing to configure.",
                size="sm",
                c="dimmed",
            ),
            dmc.Table(
                [
                    dmc.TableThead(
                        dmc.TableTr([dmc.TableTh("Column"), dmc.TableTh("Type"), dmc.TableTh("App type")])
                    ),
                    dmc.TableTbody(rows),
                ],
                striped=True,
                withTableBorder=True,
                fz="sm",
            ),
        ],
        gap="xs",
    )


def history_panel(ctx_: AppContext, file: FileDef) -> Any:
    try:
        df = ctx_.forms.file_history(file, limit=200)
    except BackendError as exc:
        return error_alert(exc)
    if df.empty:
        return empty_state(
            "No recorded uploads",
            "Files landed outside the app have no history here; every upload, replacement and deletion made "
            "in the app is recorded.",
            "tabler:history",
        )
    rows = []
    for rec in df.to_dict("records"):
        rows.append(
            dmc.TableTr(
                [
                    dmc.TableTd(str(rec.get("version", ""))),
                    dmc.TableTd(_fmt(rec.get("changed_at"))),
                    dmc.TableTd(_fmt(rec.get("changed_by"))),
                    dmc.TableTd(CHANGE_LABELS.get(rec.get("change_type"), rec.get("change_type"))),
                    dmc.TableTd(
                        human_size(rec.get("size_bytes")) if rec.get("size_bytes") is not None else "-"
                    ),
                    dmc.TableTd(f"{int(rec['row_count']):,}" if rec.get("row_count") is not None else "-"),
                ]
            )
        )
    return dmc.Stack(
        [
            dmc.Text(
                "Who uploaded, replaced or deleted the file, with its size and row count.",
                size="xs",
                c="dimmed",
            ),
            dmc.Table(
                [
                    dmc.TableThead(
                        dmc.TableTr([dmc.TableTh(h) for h in ("#", "When", "By", "Change", "Size", "Rows")])
                    ),
                    dmc.TableTbody(rows),
                ],
                striped=True,
                withTableBorder=True,
                fz="sm",
            ),
        ],
        gap="xs",
    )


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        import pandas as pd

        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    if hasattr(value, "strftime"):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def _settings_tab(file: FileDef, can_delete: bool, dbx_path: str, settings) -> dmc.Stack:
    reader = (
        f"read_files('{dbx_path}', format => 'parquet')"
        if file.format == "parquet"
        else f"read_files('{dbx_path}', format => 'csv', header => true)"
    )
    blocks: list[Any] = [
        databricks_path_block(
            "Databricks path",
            [
                ("Volume", qualified_name(settings.catalog, file.function, "_files")),
                ("File path", dbx_path),
                ("Query", f"SELECT * FROM {reader};"),
            ],
            "Copy these into a notebook, a query or a pipeline. The catalog is the one this app is configured "
            "with"
            + ("." if settings.is_databricks else " (locally the file is stored on disk, see Storage)."),
        ),
        dmc.Paper(
            dmc.Stack(
                [
                    dmc.Title("File details", order=4),
                    dmc.TextInput(
                        id=ids.FILE_SETTINGS_DISPLAY,
                        label="Display name",
                        value=file.display_name or file.title,
                    ),
                    dmc.Textarea(
                        id=ids.FILE_SETTINGS_DESC,
                        label="Description",
                        value=file.description,
                        description="Recorded in the _catalog.files registry",
                        autosize=True,
                        minRows=2,
                    ),
                    dmc.TextInput(id=ids.FILE_SETTINGS_OWNER, label="Owner", value=file.owner),
                    dmc.Group(
                        [
                            dmc.Button(
                                "Save details",
                                id=ids.FILE_SETTINGS_SAVE,
                                leftSection=icon("tabler:device-floppy"),
                            )
                        ]
                    ),
                    html.Div(id=ids.FILE_SETTINGS_RESULT),
                ],
                gap="sm",
            ),
            withBorder=True,
            p="md",
            radius="md",
        ),
        dmc.Paper(
            dmc.Stack(
                [
                    dmc.Title("Storage", order=4),
                    dmc.Code(file.path or "-", block=True),
                    dmc.Text(
                        "The file lives in the function's volume; pipelines and the Databricks CLI can read and "
                        "replace it there directly.",
                        size="xs",
                        c="dimmed",
                    ),
                ],
                gap="xs",
            ),
            withBorder=True,
            p="md",
            radius="md",
        ),
    ]
    if can_delete:
        blocks.append(
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Title("Danger zone", order=4, c="red"),
                        dmc.Text(
                            f"Delete '{file.title}' ({human_size(file.size_bytes)}). The file is removed from the volume; "
                            "the history of its uploads is kept. Only global administrators can delete files.",
                            size="sm",
                        ),
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=ids.DROP_FILE_CONFIRM,
                                    placeholder=f"type {file.name} to confirm",
                                    w=340,
                                ),
                                dmc.Button(
                                    "Delete file",
                                    id=ids.DROP_FILE_SUBMIT,
                                    color="red",
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
                style={"borderColor": "#fa5252"},
            )
        )
    else:
        blocks.append(
            dmc.Text("Deleting a file is reserved to global administrators.", size="sm", c="dimmed")
        )
    return dmc.Stack(blocks, gap="md")


def _replace_modal(ctx_: AppContext, file: FileDef) -> dmc.Modal:
    max_mb = ctx_.settings.max_file_mb
    return dmc.Modal(
        id=ids.FILE_REPLACE_MODAL,
        title=f"Replace {file.name}",
        size="xl",
        children=dmc.Stack(
            [
                dmc.Text(
                    f"Upload a new {file.format.upper()} file (up to {max_mb} MB). It replaces the stored content as a "
                    "whole; the previous size and row count stay in the history.",
                    size="sm",
                    c="dimmed",
                ),
                dcc.Upload(
                    id=ids.FILE_REPLACE_UPLOAD,
                    children=html.Div(
                        ["Drag and drop or ", html.B("click to choose"), f" a {file.format.upper()} file"]
                    ),
                    className="rdm-dropzone",
                    multiple=False,
                    accept=f".{file.format}",
                    max_size=max_mb * 1024 * 1024,
                ),
                html.Div(id=ids.FILE_REPLACE_PREVIEW),
                dmc.Group(
                    [
                        dmc.Button(
                            "Replace",
                            id=ids.FILE_REPLACE_SUBMIT,
                            disabled=True,
                            leftSection=icon("tabler:upload"),
                        )
                    ],
                    justify="flex-end",
                ),
            ],
            gap="sm",
        ),
    )


def _load(ctx_: AppContext, key: dict[str, str]) -> tuple[FileDef, Role]:
    return ctx_.forms.get_file(key["function"], key["name"]), ctx_.role_of(key["function"])


def register(app) -> None:
    @app.callback(
        Output(ids.FILE_COLUMNS_PANEL, "children"),
        Output(ids.FILE_HISTORY_PANEL, "children"),
        Input(ids.FILE_TABS, "value"),
        State(ids.FILE_KEY, "data"),
        State(ids.PERSONA, "data"),
    )
    def load_tab(tab, key, persona):
        if tab not in ("columns", "history") or not key:
            return no_update, no_update
        c = get_context(persona)
        try:
            file, _role = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return error_alert(exc), error_alert(exc)
        if tab == "columns":
            return columns_panel(c, file), no_update
        return no_update, history_panel(c, file)

    @app.callback(
        Output(ids.DOWNLOAD, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.FILE_DOWNLOAD, "n_clicks"),
        State(ids.FILE_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def download(n, key, persona):
        if not n:
            return no_update, no_update
        c = get_context(persona)
        try:
            file, _role = _load(c, key)
            data = c.forms.read_file(file)
        except (BackendError, PermissionDenied) as exc:
            return no_update, notify(str(exc), title="Cannot download", color="red")
        return dcc.send_bytes(data, file.name), no_update

    @app.callback(
        Output(ids.FILE_REPLACE_MODAL, "opened"),
        Input(ids.FILE_REPLACE_OPEN, "n_clicks"),
        prevent_initial_call=True,
    )
    def open_replace(n):
        return bool(n)

    @app.callback(
        Output(ids.FILE_REPLACE_PREVIEW, "children"),
        Output(ids.FILE_REPLACE_TOKEN, "data"),
        Output(ids.FILE_REPLACE_SUBMIT, "disabled"),
        Input(ids.FILE_REPLACE_UPLOAD, "contents"),
        State(ids.FILE_REPLACE_UPLOAD, "filename"),
        State(ids.FILE_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def preview_replacement(contents, filename, key, persona):
        if not contents:
            return no_update, no_update, True
        c = get_context(persona)
        token = uploads.put_data_url(filename or "upload", contents)
        _name, data = uploads.get(token)
        try:
            name = check_upload(filename or "", data, c.settings.max_file_mb)
            fmt = name.rsplit(".", 1)[-1]
            expected = key["name"].rsplit(".", 1)[-1]
            if fmt != expected:
                raise FileError(f"Upload a {expected.upper()} file to replace {key['name']}.")
            frame = preview_bytes(data, fmt)
        except FileError as exc:
            uploads.drop(token)
            return error_alert(exc, "Cannot use this file"), None, True
        preview = dmc.Stack(
            [
                dmc.Text(
                    f"{filename} · {human_size(len(data))} · {len(frame.columns)} columns (first {len(frame)} rows)",
                    size="sm",
                    fw=500,
                ),
                dag.AgGrid(
                    rowData=g.records_from_frame(frame),
                    columnDefs=g.frame_column_defs(frame),
                    defaultColDef={"resizable": True},
                    columnSize="autoSize",
                    className=GRID_THEME,
                    style={"height": "240px"},
                ),
            ],
            gap="xs",
        )
        return preview, token, False

    @app.callback(
        Output(ids.FILE_REPLACE_MODAL, "opened", allow_duplicate=True),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.FILE_REPLACE_SUBMIT, "n_clicks"),
        State(ids.FILE_REPLACE_TOKEN, "data"),
        State(ids.FILE_KEY, "data"),
        State(ids.URL, "pathname"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def replace_file(n, token, key, pathname, nav_version, persona):
        if not n:
            return no_update, no_update, no_update, no_update
        stored = uploads.get(token)
        if stored is None:
            return no_update, no_update, notify("Upload a file first", color="yellow"), no_update
        _filename, data = stored
        c = get_context(persona)
        try:
            file, _role = _load(c, key)
            updated = c.forms.replace_file(file, data)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return no_update, no_update, notify(str(exc), title="Not replaced", color="red"), no_update
        uploads.drop(token)
        invalidate_metadata()
        return (
            False,
            pathname,  # re-render the page with the new content
            notify(f"File replaced ({updated.row_count or 0:,} rows)"),
            (nav_version or 0) + 1,
        )

    @app.callback(
        Output(ids.FILE_SETTINGS_RESULT, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.FILE_SETTINGS_SAVE, "n_clicks"),
        State(ids.FILE_SETTINGS_DISPLAY, "value"),
        State(ids.FILE_SETTINGS_DESC, "value"),
        State(ids.FILE_SETTINGS_OWNER, "value"),
        State(ids.FILE_KEY, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def save_settings(n, display, desc, owner, key, nav_version, persona):
        if not n:
            return no_update, no_update, no_update
        c = get_context(persona)
        try:
            file, _role = _load(c, key)
            file.display_name, file.description, file.owner = (
                (display or "").strip(),
                (desc or "").strip(),
                (owner or "").strip(),
            )
            c.forms.update_file_metadata(file)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc, "Not saved"), no_update, no_update
        invalidate_metadata()
        return None, notify("Details saved"), (nav_version or 0) + 1

    @app.callback(
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.DROP_FILE_SUBMIT, "n_clicks"),
        State(ids.DROP_FILE_CONFIRM, "value"),
        State(ids.FILE_KEY, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def drop_file(n, confirm, key, nav_version, persona):
        if not n:
            return no_update, no_update, no_update
        if (confirm or "").strip() != key["name"]:
            return no_update, notify("Type the file name exactly to confirm", color="yellow"), no_update
        c = get_context(persona)
        try:
            file, _role = _load(c, key)
            c.forms.drop_file(file)
        except (BackendError, PermissionDenied) as exc:
            return no_update, notify(str(exc), title="Not deleted", color="red"), no_update
        invalidate_metadata()
        return (
            function_href(key["function"]),
            notify(f"File '{file.title}' deleted", color="gray"),
            (nav_version or 0) + 1,
        )
