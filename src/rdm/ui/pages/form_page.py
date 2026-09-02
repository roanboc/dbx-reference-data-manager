"""A form: header, then Data (AG Grid editor) / History / Schema / Settings tabs."""

from __future__ import annotations

import copy
import logging
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
import pandas as pd
from dash import Input, Output, State, ctx, dcc, html, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    FormDef,
    Role,
    ValidationIssue,
    humanize,
    sanitize_identifier,
)
from rdm.services import Draft, build_changeset_from_draft, describe_row
from rdm.services.draft import invalid_cells
from rdm.services.excel_import import ImportError_, coerce_frame, list_sheets, map_frame_to_form, read_table
from rdm.ui import grid as g
from rdm.ui import ids, uploads
from rdm.ui.components import (
    TYPE_ICONS,
    empty_state,
    error_alert,
    icon,
    info_alert,
    issues_list,
    notify,
    page_title,
    role_badge,
)
from rdm.ui.context import AppContext, get_context, invalidate_metadata
from rdm.ui.layout import domain_href

log = logging.getLogger(__name__)
TYPE_OPTIONS = [{"value": t.value, "label": f"{t.label} ({t.value})"} for t in DataType.editable_types()]
GRID_THEME = "ag-theme-quartz"


# --------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------


def render(ctx_: AppContext, domain: str, name: str) -> dmc.Stack:
    form = ctx_.forms.get_form(domain, name)
    role = ctx_.role_of(domain)
    editable = role.can_edit and form.is_editable
    try:
        domain_title = ctx_.backend.get_domain(domain).title
    except BackendError:
        domain_title = domain
    header = page_title(
        form.title,
        form.description or None,
        crumbs=[dmc.Anchor(domain_title, href=domain_href(domain)), dmc.Text(form.name)],
        right=dmc.Stack(
            [
                dmc.Group([role_badge(role)], justify="flex-end"),
                dmc.Text(_meta(form), size="xs", c="dimmed", ta="right"),
            ],
            gap=4,
        ),
    )
    tabs = [
        dmc.TabsTab("Data", value="data", leftSection=icon("tabler:table")),
        dmc.TabsTab("History", value="history", leftSection=icon("tabler:history")),
        dmc.TabsTab("Schema", value="schema", leftSection=icon("tabler:columns")),
    ]
    panels = [
        dmc.TabsPanel(_data_tab(ctx_, form, role, editable), value="data", pt="sm"),
        dmc.TabsPanel(
            html.Div(id=ids.HISTORY_PANEL, children=dmc.Loader(size="sm")), value="history", pt="sm"
        ),
        dmc.TabsPanel(html.Div(id=ids.SCHEMA_PANEL, children=dmc.Loader(size="sm")), value="schema", pt="sm"),
    ]
    if role.can_admin:
        tabs.append(dmc.TabsTab("Settings", value="settings", leftSection=icon("tabler:settings")))
        panels.append(dmc.TabsPanel(_settings_tab(form), value="settings", pt="sm"))
    return dmc.Stack(
        [
            dcc.Store(id=ids.FORM_KEY, data={"domain": domain, "form": name}),
            dcc.Store(id=ids.DRAFT, data=Draft().to_dict()),
            dcc.Store(id=ids.GRID_VERSION, data=0),
            dcc.Store(id=ids.IMPORT_TOKEN, data=None),
            header,
            dmc.Tabs([dmc.TabsList(tabs), *panels], value="data", id=ids.FORM_TABS, keepMounted=False),
            _import_modal(form),
            _schema_modals(form),
        ],
        gap="xs",
    )


def _meta(form: FormDef) -> str:
    from rdm.ui.components import form_meta

    return form_meta(form)


def _data_tab(ctx_: AppContext, form: FormDef, role: Role, editable: bool) -> dmc.Stack:
    notes = []
    if not form.is_editable:
        notes.append(
            info_alert(
                "This table was not created by the app (no `_id`/`_version` columns), so it is shown read-only."
            )
        )
    elif not role.can_edit:
        notes.append(dmc.Text("Read-only: you have Viewer access to this domain.", size="sm", c="dimmed"))
    toolbar_left = [
        dmc.TextInput(
            id=ids.GRID_SEARCH,
            placeholder="Search in any column",
            leftSection=icon("tabler:search"),
            debounce=400,
            w=260,
            size="sm",
        ),
        dmc.Switch(id=ids.GRID_AUDIT, label="Audit columns", size="sm", checked=False),
        dmc.Button(
            "Refresh", id=ids.GRID_REFRESH, variant="default", size="sm", leftSection=icon("tabler:refresh")
        ),
        dmc.Menu(
            [
                dmc.MenuTarget(
                    dmc.Button("Download", variant="default", size="sm", leftSection=icon("tabler:download"))
                ),
                dmc.MenuDropdown(
                    [
                        dmc.MenuItem(
                            "CSV (current view)", id=ids.GRID_CSV, leftSection=icon("tabler:file-type-csv")
                        ),
                        dmc.MenuItem(
                            "Excel (whole list)",
                            id=ids.GRID_XLSX,
                            leftSection=icon("tabler:file-spreadsheet"),
                        ),
                    ]
                ),
            ]
        ),
    ]
    # Editor controls are always in the layout (callbacks reference them); hidden for read-only users.
    toolbar_right = [
        dmc.Button(
            "Import rows",
            id=ids.IMPORT_OPEN,
            variant="default",
            size="sm",
            leftSection=icon("tabler:upload"),
        ),
        dmc.Button(
            "Add row",
            id=ids.GRID_ADD,
            variant="light",
            size="sm",
            leftSection=icon("tabler:row-insert-top"),
        ),
        dmc.Button(
            "Delete selected",
            id=ids.GRID_DELETE,
            variant="light",
            color="red",
            size="sm",
            leftSection=icon("tabler:trash"),
        ),
        dmc.Button(
            "Discard",
            id=ids.GRID_DISCARD,
            variant="subtle",
            size="sm",
            leftSection=icon("tabler:arrow-back-up"),
        ),
        dmc.Button(
            "Save", id=ids.GRID_SAVE, size="sm", disabled=True, leftSection=icon("tabler:device-floppy")
        ),
    ]
    n_cols = len(form.user_columns)
    return dmc.Stack(
        [
            *notes,
            dmc.Group(
                [
                    dmc.Group(toolbar_left, gap="xs"),
                    dmc.Group(toolbar_right, gap="xs", style={} if editable else {"display": "none"}),
                ],
                justify="space-between",
            ),
            dmc.Text(id=ids.GRID_CAPTION, size="xs", c="dimmed"),
            dag.AgGrid(
                id=ids.GRID,
                rowData=[],
                columnDefs=g.column_defs(form, editable, False),
                getRowId="params.data._id",
                defaultColDef=g.default_col_def(editable),
                dashGridOptions=g.grid_options(editable),
                rowClassRules=g.row_class_rules(),
                columnSize="responsiveSizeToFit" if n_cols <= 6 else "autoSize",
                className=GRID_THEME,
                style={"height": "62vh", "width": "100%"},
            ),
            html.Div(id=ids.PENDING),
            html.Div(id=ids.SAVE_RESULT),
        ],
        gap="xs",
    )


def _pending_bar(
    form: FormDef, rows: list[dict[str, Any]], draft: Draft
) -> tuple[Any, bool, dict[str, list[str]]]:
    """Pending-changes panel, whether Save is allowed, and invalid cells per row."""
    if draft.is_empty:
        return None, True, {}
    changes, issues = build_changeset_from_draft(form, rows, draft)
    review = _review_table(form, rows, changes)
    body = dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.Group(
                        [
                            icon("tabler:clock-edit", 18),
                            dmc.Text("Unsaved changes: ", fw=600, span=True),
                            dmc.Text(changes.summary(), span=True),
                        ],
                        gap=6,
                    ),
                    dmc.Text(
                        "Save writes all changes at once; Discard restores the loaded rows.",
                        size="xs",
                        c="dimmed",
                    ),
                ],
                justify="space-between",
            ),
            issues_list(issues) if issues else None,
            dmc.Accordion(
                [
                    dmc.AccordionItem(
                        [
                            dmc.AccordionControl("Review changes", icon=icon("tabler:list-details")),
                            dmc.AccordionPanel(review),
                        ],
                        value="review",
                    )
                ],
                variant="contained",
            ),
        ],
        gap="xs",
    )
    return (
        dmc.Paper(body, withBorder=True, p="sm", radius="md", className="rdm-pending"),
        bool(issues) or changes.is_empty,
        invalid_cells(issues),
    )


def _review_table(form: FormDef, rows: list[dict[str, Any]], changes: ChangeSet) -> dmc.Table:
    by_id = {str(r.get(ID_COLUMN)): r for r in rows}
    body = []
    for u in changes.updates:
        for col, new in u.changes.items():
            old = by_id.get(u.row_id, {}).get(col)
            body.append(
                dmc.TableTr(
                    [
                        dmc.TableTd(u.label),
                        dmc.TableTd("edit"),
                        dmc.TableTd(humanize(col)),
                        dmc.TableTd(_fmt(old)),
                        dmc.TableTd(_fmt(new)),
                    ]
                )
            )
    for i in changes.inserts:
        summary = ", ".join(f"{humanize(k)}={_fmt(v)}" for k, v in i.values.items() if v is not None)
        body.append(
            dmc.TableTr(
                [
                    dmc.TableTd(i.label),
                    dmc.TableTd("add"),
                    dmc.TableTd(""),
                    dmc.TableTd(""),
                    dmc.TableTd(summary[:200]),
                ]
            )
        )
    for d in changes.deletes:
        body.append(
            dmc.TableTr(
                [
                    dmc.TableTd(d.label),
                    dmc.TableTd("delete"),
                    dmc.TableTd(""),
                    dmc.TableTd(""),
                    dmc.TableTd(""),
                ]
            )
        )
    head = dmc.TableThead(dmc.TableTr([dmc.TableTh(h) for h in ("Row", "Change", "Column", "From", "To")]))
    return dmc.Table([head, dmc.TableTbody(body)], striped=True, withTableBorder=True, fz="xs")


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _settings_tab(form: FormDef) -> dmc.Stack:
    return dmc.Stack(
        [
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Title("Form details", order=4),
                        dmc.TextInput(
                            id=ids.SETTINGS_DISPLAY,
                            label="Display name",
                            value=form.display_name or form.title,
                        ),
                        dmc.Textarea(
                            id=ids.SETTINGS_DESC,
                            label="Description",
                            value=form.description,
                            description="Stored as the table comment",
                            autosize=True,
                            minRows=2,
                        ),
                        dmc.TextInput(
                            id=ids.SETTINGS_OWNER,
                            label="Owner",
                            value=form.owner,
                            description="Stored as a table property and tag",
                        ),
                        dmc.Group(
                            [
                                dmc.Button(
                                    "Save details",
                                    id=ids.SETTINGS_SAVE,
                                    leftSection=icon("tabler:device-floppy"),
                                )
                            ]
                        ),
                        html.Div(id=ids.SETTINGS_RESULT),
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
                        dmc.Title("Properties and tags", order=4),
                        dmc.Text(
                            "Table properties (TBLPROPERTIES) and tags as stored in the catalog.",
                            size="sm",
                            c="dimmed",
                        ),
                        dmc.SimpleGrid(
                            [
                                dmc.Stack(
                                    [
                                        dmc.Text("Properties", fw=600, size="sm"),
                                        dmc.Code(_kv(form.properties), block=True),
                                    ],
                                    gap=4,
                                ),
                                dmc.Stack(
                                    [
                                        dmc.Text("Tags", fw=600, size="sm"),
                                        dmc.Code(_kv(form.tags), block=True),
                                    ],
                                    gap=4,
                                ),
                            ],
                            cols={"base": 1, "md": 2},
                        ),
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
                        dmc.Title("Danger zone", order=4, c="red"),
                        dmc.Text(
                            f"Delete '{form.title}' and all of its {form.row_count or 0:,} rows. This cannot be undone.",
                            size="sm",
                        ),
                        dmc.Group(
                            [
                                dmc.TextInput(
                                    id=ids.DROP_FORM_CONFIRM,
                                    placeholder=f"type {form.name} to confirm",
                                    w=320,
                                ),
                                dmc.Button(
                                    "Delete form",
                                    id=ids.DROP_FORM_SUBMIT,
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
            ),
        ],
        gap="md",
    )


def _kv(d: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in sorted(d.items())) or "(none)"


def _import_modal(form: FormDef) -> dmc.Modal:
    return dmc.Modal(
        id=ids.IMPORT_MODAL,
        title="Import rows",
        size="xl",
        children=dmc.Stack(
            [
                dmc.Text(
                    f"Rows are appended to '{form.title}'. Headers are matched to column names (spaces and punctuation are ignored). Existing rows are never modified.",
                    size="sm",
                    c="dimmed",
                ),
                dcc.Upload(
                    id=ids.IMPORT_UPLOAD,
                    children=html.Div(
                        ["Drag and drop or ", html.B("click to choose"), " an Excel or CSV file"]
                    ),
                    className="rdm-dropzone",
                    multiple=False,
                    accept=".xlsx,.xls,.csv,.tsv",
                ),
                dmc.Select(
                    id=ids.IMPORT_SHEET,
                    label="Sheet",
                    data=[],
                    value=None,
                    style={"display": "none"},
                    searchable=True,
                ),
                html.Div(id=ids.IMPORT_PREVIEW),
                dmc.Group(
                    [
                        dmc.Button(
                            "Append rows",
                            id=ids.IMPORT_SUBMIT,
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


def _schema_modals(form: FormDef) -> html.Div:
    return html.Div(
        [
            dmc.Modal(
                id=ids.ADD_COL_MODAL,
                title="Add column",
                children=dmc.Stack(
                    [
                        dmc.TextInput(
                            id=ids.ADD_COL_NAME,
                            label="Column name",
                            placeholder="e.g. Effective date",
                            required=True,
                        ),
                        dmc.Select(
                            id=ids.ADD_COL_TYPE,
                            label="Type",
                            data=TYPE_OPTIONS,
                            value="STRING",
                            allowDeselect=False,
                        ),
                        dmc.TextInput(id=ids.ADD_COL_DESC, label="Description"),
                        dmc.Group(
                            [
                                dmc.NumberInput(
                                    id=ids.ADD_COL_PRECISION,
                                    label="Precision",
                                    value=18,
                                    min=1,
                                    max=38,
                                    w=140,
                                ),
                                dmc.NumberInput(
                                    id=ids.ADD_COL_SCALE, label="Scale", value=4, min=0, max=38, w=140
                                ),
                            ]
                        ),
                        dmc.Text(
                            "New columns are optional (existing rows have no value); mark them required later in the schema grid.",
                            size="xs",
                            c="dimmed",
                        ),
                        dmc.Group(
                            [dmc.Button("Add", id=ids.ADD_COL_SUBMIT, leftSection=icon("tabler:plus"))],
                            justify="flex-end",
                        ),
                    ],
                    gap="sm",
                ),
            ),
            dmc.Modal(
                id=ids.DROP_COL_MODAL,
                title="Remove column",
                children=dmc.Stack(
                    [
                        dmc.Alert(
                            "Removing a column deletes its values in every row. This cannot be undone.",
                            color="red",
                            variant="light",
                            icon=icon("tabler:alert-triangle"),
                        ),
                        dmc.Select(
                            id=ids.DROP_COL_SELECT,
                            label="Column",
                            searchable=True,
                            data=[
                                {"value": c.name, "label": f"{humanize(c.name)} ({c.name})"}
                                for c in form.user_columns
                            ],
                        ),
                        dmc.TextInput(id=ids.DROP_COL_CONFIRM, label="Type the column name to confirm"),
                        dmc.Group(
                            [
                                dmc.Button(
                                    "Remove column",
                                    id=ids.DROP_COL_SUBMIT,
                                    color="red",
                                    leftSection=icon("tabler:trash-x"),
                                )
                            ],
                            justify="flex-end",
                        ),
                    ],
                    gap="sm",
                ),
            ),
        ]
    )


# --------------------------------------------------------------------------------------
# History and schema tabs (rendered lazily)
# --------------------------------------------------------------------------------------


def history_panel(ctx_: AppContext, form: FormDef) -> Any:
    try:
        df = ctx_.forms.history(form, limit=500)
    except BackendError as exc:
        return error_alert(exc)
    if df.empty:
        return empty_state(
            "No changes yet", "Every save is recorded here with who changed what and when.", "tabler:history"
        )
    labels = {"insert": "Added", "update": "Edited", "delete": "Deleted"}
    df = df.assign(change_type=df["change_type"].map(labels).fillna(df["change_type"]))
    rows = g.rows_to_records(df, form)
    note = (
        "Backed by the audit table in the catalog (Change Data Feed as fallback)."
        if ctx_.settings.is_databricks
        else "Locally this is the _catalog.change_log table; in Databricks it is the audit table."
    )
    return dmc.Stack(
        [
            dmc.Group(
                [
                    dmc.TextInput(
                        id=ids.HISTORY_FILTER,
                        placeholder="Search the history",
                        leftSection=icon("tabler:search"),
                        debounce=250,
                        w=300,
                        size="sm",
                    ),
                    dmc.Text(
                        "Edits show the row after the change; deletions show the row as it was. " + note,
                        size="xs",
                        c="dimmed",
                    ),
                ],
                gap="md",
            ),
            dag.AgGrid(
                id=ids.HISTORY_GRID,
                rowData=rows,
                columnDefs=g.history_column_defs(form),
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={"rowHeight": 32, "enableCellTextSelection": True},
                columnSize="autoSize",
                className=GRID_THEME,
                style={"height": "58vh"},
            ),
        ],
        gap="xs",
    )


def schema_panel(ctx_: AppContext, form: FormDef, role: Role) -> Any:
    rows = [
        {
            "name": c.name,
            "type": c.type_label,
            "description": c.description,
            "required": c.required,
            "key": c.is_key,
            "options": ", ".join(c.options),
        }
        for c in form.user_columns
    ]
    admin = role.can_admin and form.is_editable
    defs = [
        {"field": "name", "headerName": "Column", "editable": False, "pinned": "left", "minWidth": 160},
        {"field": "type", "headerName": "Type", "editable": False, "maxWidth": 150},
        {
            "field": "description",
            "headerName": "Description",
            "editable": admin,
            "flex": 2,
            "minWidth": 220,
            "headerTooltip": "Shown as a tooltip in the grid",
        },
        {
            "field": "required",
            "headerName": "Required",
            "editable": admin,
            "cellRenderer": "agCheckboxCellRenderer",
            "cellEditor": "agCheckboxCellEditor",
            "maxWidth": 110,
        },
        {
            "field": "key",
            "headerName": "Business key",
            "editable": admin,
            "cellRenderer": "agCheckboxCellRenderer",
            "cellEditor": "agCheckboxCellEditor",
            "maxWidth": 130,
        },
        {
            "field": "options",
            "headerName": "Allowed values",
            "editable": admin,
            "minWidth": 200,
            "headerTooltip": "Comma-separated; text columns only",
        },
    ]
    sys_cols = [c for c in form.columns if c.is_system]
    blocks = [
        dmc.Text(
            "Column names and types are fixed once a form exists (changing a type would rewrite the table). "
            + (
                "Administrators can edit descriptions, mark columns as required or as business key, restrict values to a list, add and remove columns."
                if admin
                else ""
            ),
            size="sm",
            c="dimmed",
        ),
        dag.AgGrid(
            id=ids.SCHEMA_GRID,
            rowData=rows,
            columnDefs=defs,
            getRowId="params.data.name",
            defaultColDef={"resizable": True, "sortable": False},
            dashGridOptions={"rowHeight": 34, "stopEditingWhenCellsLoseFocus": True, "singleClickEdit": True},
            columnSize="responsiveSizeToFit",
            className=GRID_THEME,
            style={"height": f"{min(120 + 34 * len(rows), 520)}px"},
        ),
    ]
    if admin:
        blocks.append(
            dmc.Group(
                [
                    dmc.Button("Save schema", id=ids.SCHEMA_SAVE, leftSection=icon("tabler:device-floppy")),
                    dmc.Button(
                        "Add column", id=ids.ADD_COL_OPEN, variant="light", leftSection=icon("tabler:plus")
                    ),
                    dmc.Button(
                        "Remove column",
                        id=ids.DROP_COL_OPEN,
                        variant="light",
                        color="red",
                        leftSection=icon("tabler:trash"),
                    ),
                ],
                gap="xs",
            )
        )
        blocks.append(html.Div(id=ids.SCHEMA_RESULT))
    if sys_cols:
        blocks.append(
            dmc.Accordion(
                [
                    dmc.AccordionItem(
                        [
                            dmc.AccordionControl("System columns", icon=icon("tabler:settings-automation")),
                            dmc.AccordionPanel(
                                dmc.List(
                                    [
                                        dmc.ListItem(
                                            dmc.Group(
                                                [
                                                    icon(TYPE_ICONS[c.data_type], 14),
                                                    dmc.Code(c.name),
                                                    dmc.Text(c.description or c.type_label, size="sm"),
                                                ],
                                                gap=6,
                                            )
                                        )
                                        for c in sys_cols
                                    ],
                                    size="sm",
                                )
                            ),
                        ],
                        value="sys",
                    )
                ],
                variant="separated",
            )
        )
    return dmc.Stack(blocks, gap="sm")


# --------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------


def _load(ctx_: AppContext, key: dict[str, str]) -> tuple[FormDef, Role, bool]:
    form = ctx_.forms.get_form(key["domain"], key["form"])
    role = ctx_.role_of(key["domain"])
    return form, role, role.can_edit and form.is_editable


def register(app) -> None:
    @app.callback(
        Output(ids.GRID, "rowData"),
        Output(ids.GRID, "columnDefs"),
        Output(ids.GRID_CAPTION, "children"),
        Output(ids.PENDING, "children", allow_duplicate=True),
        Output(ids.GRID_SAVE, "disabled", allow_duplicate=True),
        Input(ids.FORM_KEY, "data"),
        Input(ids.GRID_SEARCH, "value"),
        Input(ids.GRID_AUDIT, "checked"),
        Input(ids.GRID_REFRESH, "n_clicks"),
        Input(ids.GRID_VERSION, "data"),
        State(ids.DRAFT, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call="initial_duplicate",
    )
    def load_grid(key, search, audit, _refresh, _version, draft_raw, persona):
        if not key:
            return no_update, no_update, no_update, no_update, no_update
        c = get_context(persona)
        try:
            form, role, editable = _load(c, key)
            df = c.forms.load_rows(form, search=search or None, limit=c.settings.max_rows)
        except (BackendError, PermissionDenied) as exc:
            return [], [], f"Could not load rows: {exc}", None, True
        rows = g.rows_to_records(df, form)
        draft = Draft.from_dict(draft_raw)
        rows = draft.apply_to_rows(rows, form)
        pending, save_disabled, invalid = _pending_bar(form, rows, draft)
        for r in rows:
            g.flag_invalid(r, invalid.get(str(r.get(ID_COLUMN))))
        total = form.row_count if form.row_count is not None else len(df)
        if search:
            caption = f"{len(df):,} matching rows (of {total:,})"
        elif len(df) < total:
            caption = f"Showing the first {len(df):,} of {total:,} rows (RDM_MAX_ROWS) - use the search to find the rest"
        elif len(df) == 0:
            caption = "This list has no rows yet." + (" Use Add row or Import rows." if editable else "")
        else:
            caption = f"{len(df):,} rows" + (
                " · double-click a cell to edit, Enter to move down, Ctrl+Z to undo" if editable else ""
            )
        return rows, g.column_defs(form, editable, bool(audit)), caption, pending, save_disabled

    @app.callback(
        Output(ids.DRAFT, "data"),
        Output(ids.PENDING, "children", allow_duplicate=True),
        Output(ids.GRID, "rowTransaction"),
        Output(ids.GRID_VERSION, "data", allow_duplicate=True),
        Output(ids.GRID_SAVE, "disabled", allow_duplicate=True),
        Output(ids.SAVE_RESULT, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.GRID, "deselectAll"),
        Input(ids.GRID, "cellValueChanged"),
        Input(ids.GRID_ADD, "n_clicks"),
        Input(ids.GRID_DELETE, "n_clicks"),
        Input(ids.GRID_DISCARD, "n_clicks"),
        Input(ids.GRID_SAVE, "n_clicks"),
        State(ids.DRAFT, "data"),
        State(ids.GRID, "rowData"),
        State(ids.GRID, "selectedRows"),
        State(ids.GRID_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def grid_action(events, _add, _delete, _discard, _save, draft_raw, rows, selected, version, key, persona):
        trigger = ctx.triggered_id
        fired = {
            ids.GRID: bool(events),
            ids.GRID_ADD: bool(_add),
            ids.GRID_DELETE: bool(_delete),
            ids.GRID_DISCARD: bool(_discard),
            ids.GRID_SAVE: bool(_save),
        }
        if not fired.get(trigger):
            return (no_update,) * 8  # components were just mounted, nothing was clicked
        c = get_context(persona)
        try:
            form, _role, editable = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return (no_update,) * 5 + (error_alert(exc), no_update, no_update)
        if not editable:
            return (no_update,) * 5 + (
                error_alert("You cannot edit this form.", "Access denied"),
                no_update,
                no_update,
            )
        draft = Draft.from_dict(draft_raw)
        rows = rows or []
        transaction: Any = no_update
        result_block: Any = no_update
        notifications: Any = no_update

        if trigger == ids.GRID and events:
            touched: dict[str, dict[str, Any]] = {}
            for e in events if isinstance(events, list) else [events]:
                data = e.get("data") or {}
                rid = str(e.get("rowId") or data.get(ID_COLUMN))
                col = e.get("colId")
                if not rid or not col or col in (ID_COLUMN, VERSION_COLUMN):
                    continue
                draft.set_cell(rid, col, e.get("value"), data.get(VERSION_COLUMN))
                touched[rid] = data
            pending, save_disabled, invalid = _pending_bar(form, rows, draft)
            updates = []
            for rid, data in touched.items():
                row = {k: v for k, v in data.items() if not k.startswith(g.INVALID_PREFIX)}
                g.flag_invalid(row, invalid.get(rid))
                updates.append(row)
            transaction = {"update": updates} if updates else no_update
            return (
                draft.to_dict(),
                pending,
                transaction,
                no_update,
                save_disabled,
                no_update,
                no_update,
                no_update,
            )

        if trigger == ids.GRID_ADD:
            rid = draft.add_row()
            row = {c_.name: None for c_ in form.columns}
            row[ID_COLUMN] = rid
            row[VERSION_COLUMN] = None
            row[g.NEW_FLAG] = True
            transaction = {"add": [row], "addIndex": 0}
            pending, save_disabled, _ = _pending_bar(form, rows, draft)
            return (
                draft.to_dict(),
                pending,
                transaction,
                no_update,
                save_disabled,
                no_update,
                no_update,
                no_update,
            )

        if trigger == ids.GRID_DELETE:
            if not selected:
                return (no_update,) * 6 + (
                    notify("Select rows first (tick the boxes in the first column).", color="yellow"),
                    no_update,
                )
            for r in selected:
                rid = str(r.get(ID_COLUMN))
                draft.mark_deleted(
                    rid, r.get(VERSION_COLUMN), describe_row(form, r, fallback=f"row {rid[:8]}")
                )
            transaction = {"remove": [{ID_COLUMN: str(r.get(ID_COLUMN))} for r in selected]}
            pending, save_disabled, _ = _pending_bar(form, rows, draft)
            return draft.to_dict(), pending, transaction, no_update, save_disabled, no_update, no_update, True

        if trigger == ids.GRID_DISCARD:
            draft.clear()
            return (
                draft.to_dict(),
                None,
                no_update,
                (version or 0) + 1,
                True,
                None,
                notify("Changes discarded", color="gray"),
                True,
            )

        if trigger == ids.GRID_SAVE:
            changes, issues = build_changeset_from_draft(form, rows, draft)
            if issues:
                pending, save_disabled, _ = _pending_bar(form, rows, draft)
                return (
                    no_update,
                    pending,
                    no_update,
                    no_update,
                    True,
                    no_update,
                    notify("Fix the highlighted problems first", color="red"),
                    no_update,
                )
            try:
                result = c.forms.save(form, changes)
            except (BackendError, PermissionDenied, ValueError) as exc:
                return (
                    no_update,
                    no_update,
                    no_update,
                    no_update,
                    no_update,
                    error_alert(exc, "Nothing was saved"),
                    no_update,
                    no_update,
                )
            draft.clear()
            if result.conflicts:
                result_block = dmc.Alert(
                    dmc.Stack(
                        [
                            dmc.Text(
                                "Some rows were not saved because they changed since you loaded them. The grid has been refreshed; please re-apply those edits:",
                                size="sm",
                            ),
                            issues_list([ValidationIssue(x, None, "") for x in result.conflicts]),
                        ],
                        gap=4,
                    ),
                    title="Conflicts",
                    color="yellow",
                    variant="light",
                    icon=icon("tabler:alert-triangle"),
                    withCloseButton=True,
                )
            else:
                result_block = None
            notifications = (
                notify(f"Saved: {result.summary()}")
                if result.applied
                else notify("Nothing changed", color="gray")
            )
            return (
                draft.to_dict(),
                None,
                no_update,
                (version or 0) + 1,
                True,
                result_block,
                notifications,
                True,
            )
        return (no_update,) * 8

    @app.callback(
        Output(ids.GRID, "exportDataAsCsv"), Input(ids.GRID_CSV, "n_clicks"), prevent_initial_call=True
    )
    def export_csv(n):
        return bool(n)

    @app.callback(
        Output(ids.DOWNLOAD, "data", allow_duplicate=True),
        Input(ids.GRID_XLSX, "n_clicks"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def export_xlsx(n, key, persona):
        if not n:
            return no_update
        c = get_context(persona)
        form, _r, _e = _load(c, key)
        df = c.forms.load_rows(form, limit=max(c.settings.max_rows, 100_000))
        cols = [col.name for col in form.user_columns if col.name in df.columns]

        def write(buffer):
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                df[cols].to_excel(writer, index=False, sheet_name=(form.name[:31] or "data"))

        return dcc.send_bytes(write, f"{form.name}.xlsx")

    @app.callback(
        Output(ids.HISTORY_PANEL, "children"),
        Output(ids.SCHEMA_PANEL, "children"),
        Input(ids.FORM_TABS, "value"),
        Input(ids.GRID_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
    )
    def load_tab(tab, _version, key, persona):
        if tab not in ("history", "schema") or not key:
            return no_update, no_update
        c = get_context(persona)
        try:
            form, role, _e = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return error_alert(exc), error_alert(exc)
        if tab == "history":
            return history_panel(c, form), no_update
        return no_update, schema_panel(c, form, role)

    # -- schema editing ----------------------------------------------------------------------

    @app.callback(
        Output(ids.SCHEMA_RESULT, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.GRID_VERSION, "data", allow_duplicate=True),
        Input(ids.SCHEMA_SAVE, "n_clicks"),
        State(ids.SCHEMA_GRID, "virtualRowData"),
        State(ids.SCHEMA_GRID, "rowData"),
        State(ids.GRID_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def save_schema(n, virtual_rows, row_data, version, key, persona):
        if not n:
            return no_update, no_update, no_update
        rows = virtual_rows or row_data or []
        c = get_context(persona)
        try:
            form, _role, _e = _load(c, key)
            updated = copy.deepcopy(form)
            problems = []
            for rec in rows:
                col = updated.column(rec.get("name"))
                if col is None:
                    continue
                col.description = (rec.get("description") or "").strip()
                col.nullable = not bool(rec.get("required"))
                col.is_key = bool(rec.get("key"))
                raw = (rec.get("options") or "").strip()
                opts = [o.strip() for o in raw.split(",") if o.strip()] if raw else []
                if opts and col.data_type is not DataType.STRING:
                    problems.append(f"'{col.name}': allowed values are only supported for text columns.")
                col.options = list(dict.fromkeys(opts))
            if problems:
                return error_alert(" ".join(problems), "Not saved"), no_update, no_update
            c.forms.update_form_metadata(updated)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc, "Not saved"), no_update, no_update
        invalidate_metadata()
        return None, notify("Schema saved"), (version or 0) + 1

    @app.callback(
        Output(ids.ADD_COL_MODAL, "opened"), Input(ids.ADD_COL_OPEN, "n_clicks"), prevent_initial_call=True
    )
    def open_add_col(n):
        return bool(n)

    @app.callback(
        Output(ids.DROP_COL_MODAL, "opened"), Input(ids.DROP_COL_OPEN, "n_clicks"), prevent_initial_call=True
    )
    def open_drop_col(n):
        return bool(n)

    @app.callback(
        Output(ids.ADD_COL_MODAL, "opened", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Input(ids.ADD_COL_SUBMIT, "n_clicks"),
        State(ids.ADD_COL_NAME, "value"),
        State(ids.ADD_COL_TYPE, "value"),
        State(ids.ADD_COL_DESC, "value"),
        State(ids.ADD_COL_PRECISION, "value"),
        State(ids.ADD_COL_SCALE, "value"),
        State(ids.FORM_KEY, "data"),
        State(ids.URL, "pathname"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def add_column(n, raw_name, type_value, desc, precision, scale, key, pathname, persona):
        if not n:
            return no_update, no_update, no_update
        if not (raw_name or "").strip():
            return no_update, notify("A column name is required", color="red"), no_update
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            col = ColumnDef(
                sanitize_identifier(raw_name),
                DataType(type_value or "STRING"),
                (desc or "").strip(),
                precision=int(precision or 18),
                scale=int(scale or 0),
            )
            c.forms.add_column(form, col)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return no_update, notify(str(exc), title="Not added", color="red"), no_update
        invalidate_metadata()
        return False, notify(f"Column '{col.name}' added"), pathname  # re-render the page

    @app.callback(
        Output(ids.DROP_COL_MODAL, "opened", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Input(ids.DROP_COL_SUBMIT, "n_clicks"),
        State(ids.DROP_COL_SELECT, "value"),
        State(ids.DROP_COL_CONFIRM, "value"),
        State(ids.FORM_KEY, "data"),
        State(ids.URL, "pathname"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def drop_column(n, target, confirm, key, pathname, persona):
        if not n:
            return no_update, no_update, no_update
        if not target or (confirm or "").strip() != target:
            return no_update, notify("Type the column name exactly to confirm", color="yellow"), no_update
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            c.forms.drop_column(form, target)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return no_update, notify(str(exc), title="Not removed", color="red"), no_update
        invalidate_metadata()
        return False, notify(f"Column '{target}' removed"), pathname

    # -- settings --------------------------------------------------------------------------

    @app.callback(
        Output(ids.SETTINGS_RESULT, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.SETTINGS_SAVE, "n_clicks"),
        State(ids.SETTINGS_DISPLAY, "value"),
        State(ids.SETTINGS_DESC, "value"),
        State(ids.SETTINGS_OWNER, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def save_settings(n, display, desc, owner, nav_version, key, persona):
        if not n:
            return no_update, no_update, no_update
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            updated = copy.deepcopy(form)
            updated.display_name, updated.description, updated.owner = (
                (display or "").strip(),
                (desc or "").strip(),
                (owner or "").strip(),
            )
            c.forms.update_form_metadata(updated)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc, "Not saved"), no_update, no_update
        invalidate_metadata()
        return None, notify("Details saved"), (nav_version or 0) + 1

    @app.callback(
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.DROP_FORM_SUBMIT, "n_clicks"),
        State(ids.DROP_FORM_CONFIRM, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def drop_form(n, confirm, nav_version, key, persona):
        if not n:
            return no_update, no_update, no_update
        if (confirm or "").strip() != key["form"]:
            return no_update, notify("Type the form name exactly to confirm", color="yellow"), no_update
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            c.forms.drop_form(form)
        except (BackendError, PermissionDenied) as exc:
            return no_update, notify(str(exc), title="Not deleted", color="red"), no_update
        invalidate_metadata()
        return (
            domain_href(key["domain"]),
            notify(f"Form '{form.title}' deleted", color="gray"),
            (nav_version or 0) + 1,
        )

    # -- import rows -----------------------------------------------------------------------

    @app.callback(
        Output(ids.IMPORT_MODAL, "opened"), Input(ids.IMPORT_OPEN, "n_clicks"), prevent_initial_call=True
    )
    def open_import(n):
        return bool(n)

    @app.callback(
        Output(ids.IMPORT_PREVIEW, "children"),
        Output(ids.IMPORT_TOKEN, "data"),
        Output(ids.IMPORT_SHEET, "data"),
        Output(ids.IMPORT_SHEET, "value"),
        Output(ids.IMPORT_SHEET, "style"),
        Output(ids.IMPORT_SUBMIT, "disabled"),
        Input(ids.IMPORT_UPLOAD, "contents"),
        Input(ids.IMPORT_SHEET, "value"),
        State(ids.IMPORT_UPLOAD, "filename"),
        State(ids.IMPORT_TOKEN, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def preview_import(contents, sheet, filename, token, key, persona):
        if ctx.triggered_id == ids.IMPORT_UPLOAD:
            if not contents:
                return no_update, no_update, no_update, no_update, no_update, True
            token = uploads.put_data_url(filename or "upload", contents)
            sheet = None
        stored = uploads.get(token)
        if stored is None:
            return info_alert("Upload a file to continue."), None, [], None, {"display": "none"}, True
        name, data = stored
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            sheets = list_sheets(data, name)
            sheet = sheet if sheet in sheets else sheets[0]
            raw = read_table(data, name, sheet=None if sheet == "data" else sheet)
        except (ImportError_, ValueError, BackendError) as exc:
            return error_alert(exc, "Cannot read the file"), token, [], None, {"display": "none"}, True
        sheet_data = [{"value": s, "label": s} for s in sheets]
        sheet_style = {} if len(sheets) > 1 else {"display": "none"}
        mapping, unmatched = map_frame_to_form(raw, form)
        if not mapping:
            return (
                error_alert(
                    "None of the file's headers match this form's columns. Expected: "
                    + ", ".join(c_.name for c_ in form.user_columns),
                    "No matching columns",
                ),
                token,
                sheet_data,
                sheet,
                sheet_style,
                True,
            )
        frame, issues = coerce_frame(raw, form.user_columns, mapping)
        blocks = []
        if unmatched:
            blocks.append(
                dmc.Alert(
                    "No matching header for: " + ", ".join(unmatched) + " (those values will be empty).",
                    color="yellow",
                    variant="light",
                )
            )
        extra = [h for h in raw.columns if h not in mapping.values()]
        if extra:
            blocks.append(dmc.Text("Ignored columns in the file: " + ", ".join(extra), size="xs", c="dimmed"))
        preview_rows = g.rows_to_records(frame.head(15), form)
        blocks.append(
            dag.AgGrid(
                rowData=preview_rows,
                columnDefs=[{"field": c_.name, "headerName": humanize(c_.name)} for c_ in form.user_columns],
                defaultColDef={"resizable": True},
                columnSize="autoSize",
                className=GRID_THEME,
                style={"height": "300px"},
            )
        )
        blocks.append(dmc.Text(f"{len(frame):,} rows in the file (showing the first 15).", size="sm"))
        if issues:
            blocks.append(
                dmc.Alert(
                    dmc.Stack(
                        [
                            dmc.Text(
                                f"{len(issues)} problem(s). Fix the file, or import anyway and leave the invalid cells empty.",
                                size="sm",
                            ),
                            issues_list(issues),
                        ],
                        gap=4,
                    ),
                    color="red",
                    variant="light",
                )
            )
        return dmc.Stack(blocks, gap="xs"), token, sheet_data, sheet, sheet_style, frame.empty

    @app.callback(
        Output(ids.IMPORT_MODAL, "opened", allow_duplicate=True),
        Output(ids.GRID_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.IMPORT_SUBMIT, "n_clicks"),
        State(ids.IMPORT_TOKEN, "data"),
        State(ids.IMPORT_SHEET, "value"),
        State(ids.GRID_VERSION, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def submit_import(n, token, sheet, version, nav_version, key, persona):
        if not n:
            return no_update, no_update, no_update, no_update
        stored = uploads.get(token)
        if stored is None:
            return no_update, no_update, notify("Upload a file first", color="yellow"), no_update
        name, data = stored
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            raw = read_table(data, name, sheet=None if sheet in (None, "data") else sheet)
            mapping, _unmatched = map_frame_to_form(raw, form)
            frame, _issues = coerce_frame(raw, form.user_columns, mapping)
            count = c.forms.append_rows(form, frame)
        except (ImportError_, BackendError, PermissionDenied, ValueError) as exc:
            return no_update, no_update, notify(str(exc), title="Import failed", color="red"), no_update
        uploads.drop(token)
        invalidate_metadata()
        return False, (version or 0) + 1, notify(f"Imported {count:,} rows"), (nav_version or 0) + 1

    @app.callback(
        Output(ids.HISTORY_GRID, "dashGridOptions"),
        Input(ids.HISTORY_FILTER, "value"),
        prevent_initial_call=True,
    )
    def filter_history(text):
        return {"rowHeight": 32, "enableCellTextSelection": True, "quickFilterText": text or ""}
