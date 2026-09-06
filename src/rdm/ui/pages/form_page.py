"""A form: header, then Data (AG Grid editor) / History / Schema / Settings tabs.

Editing model: every change lands in a browser-side :class:`~rdm.services.draft.Draft`
(cell edits, added and deleted rows, bulk updates, item-form edits, restored versions) and
is written in one atomic save. The Data tab stays mounted while other tabs are open so
pending changes, selection and scroll position survive a tab round trip.
"""

from __future__ import annotations

import copy
import logging
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
import pandas as pd
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.coercion import CoercionError, coerce_value
from rdm.models import (
    AUDIT_COLUMNS,
    ID_COLUMN,
    SCD2_END_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    FormDef,
    Role,
    ValidationIssue,
    humanize,
    qualified_name,
    sanitize_identifier,
    scd2_table_name,
)
from rdm.services import Draft, build_changeset_from_draft, build_import_changeset, describe_row
from rdm.services.draft import bulk_value, invalid_cells, is_temp_id, json_safe, restore_row, same_value
from rdm.services.excel_import import ImportError_, coerce_frame, list_sheets, map_frame_to_form, read_table
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
    issues_list,
    notify,
    page_title,
    role_badge,
)
from rdm.ui.context import AppContext, get_context, get_settings, invalidate_metadata
from rdm.ui.layout import function_href

log = logging.getLogger(__name__)
TYPE_OPTIONS = [{"value": t.value, "label": f"{t.label} ({t.value})"} for t in DataType.editable_types()]
GRID_THEME = g.GRID_THEME
CHANGE_LABELS = {"insert": "Added", "update": "Edited", "delete": "Deleted"}
BOOL_OPTIONS = [{"value": "true", "label": "Yes"}, {"value": "false", "label": "No"}]


# --------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------


def render(ctx_: AppContext, function: str, name: str) -> dmc.Stack:
    form = ctx_.forms.get_form(function, name)
    role = ctx_.role_of(function)
    editable = role.can_edit and form.is_editable
    try:
        function_title = ctx_.backend.get_function(function).title
    except BackendError:
        function_title = function
    table_path = qualified_name(ctx_.settings.catalog, function, name)
    header = page_title(
        form.title,
        form.description or None,
        crumbs=[dmc.Anchor(function_title, href=function_href(function)), dmc.Text(form.name)],
        right=dmc.Stack(
            [
                dmc.Group([role_badge(role)], justify="flex-end"),
                dmc.Text(_meta(form), size="xs", c="dimmed", ta="right"),
                dmc.Group([copy_code(table_path)], justify="flex-end"),
            ],
            gap=4,
            align="flex-end",
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
        panels.append(
            dmc.TabsPanel(
                _settings_tab(form, ctx_.permissions.can_delete, table_path, ctx_.settings.is_databricks),
                value="settings",
                pt="sm",
            )
        )
    return dmc.Stack(
        [
            dcc.Store(id=ids.FORM_KEY, data={"function": function, "form": name}),
            dcc.Store(id=ids.DRAFT, data=Draft().to_dict()),
            dcc.Store(id=ids.GRID_VERSION, data=0),
            dcc.Store(id=ids.IMPORT_TOKEN, data=None),
            dcc.Store(id=ids.ITEM_ROW, data=None),
            dcc.Store(id=ids.ITEM_HISTORY, data=[]),
            header,
            # keepMounted keeps the grid, its draft overlay and the Save state alive while the
            # user looks at History / Schema / Settings (the panels are hidden, not destroyed).
            dmc.Tabs([dmc.TabsList(tabs), *panels], value="data", id=ids.FORM_TABS, keepMounted=True),
            _import_modal(form),
            _bulk_modal(form, editable),
            _item_modal(),
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
        notes.append(dmc.Text("Read-only: you have Viewer access to this function.", size="sm", c="dimmed"))
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
        dmc.Button(
            "Open row",
            id=ids.ITEM_OPEN,
            variant="default",
            size="sm",
            leftSection=icon("tabler:forms"),
            disabled=not form.is_editable,
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
            "Bulk update",
            id=ids.BULK_OPEN,
            variant="light",
            size="sm",
            leftSection=icon("tabler:replace"),
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


def _settings_tab(form: FormDef, can_delete: bool, table_path: str, is_databricks: bool) -> dmc.Stack:
    scd2_path = table_path.rsplit(".", 1)[0] + f".`{scd2_table_name(form.name)}`"
    blocks: list[Any] = [
        databricks_path_block(
            "Databricks path",
            [
                ("Table (catalog.schema.table)", table_path),
                ("Query", f"SELECT * FROM {table_path};"),
                (
                    "Change feed (SCD Type 2 source)",
                    f"SELECT * FROM table_changes('{table_path.replace('`', '')}', 0);",
                ),
            ],
            "Copy these into a notebook, a query or a pipeline. The catalog is the one this app is configured "
            "with"
            + ("." if is_databricks else " (locally the data lives in DuckDB, the names are the same)."),
        ),
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
                        required=True,
                    ),
                    dmc.TextInput(
                        id=ids.SETTINGS_OWNER,
                        label="Owner",
                        value=form.owner,
                        description="Team or person accountable for this list; stored as a table property and tag",
                        required=True,
                    ),
                    dmc.TextInput(
                        id=ids.SETTINGS_OWNER_EMAIL,
                        label="Owner e-mail",
                        value=form.owner_email,
                        description="Optional contact e-mail of the owning team or person",
                        placeholder="team@example.org",
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
                    dmc.Title("History table (SCD Type 2)", order=4),
                    dmc.Text(
                        "Optional: maintain a Type 2 history table next to this form in the organisation's "
                        "notation (__START_AT / __END_AT; the current version has __END_AT IS NULL). Every "
                        "save closes and opens validity windows, so consumers query ready-made history "
                        "instead of deriving it from the change feed.",
                        size="sm",
                        c="dimmed",
                    ),
                    dmc.Switch(
                        id=ids.SCD2_SWITCH,
                        label="Maintain the history table",
                        checked=form.scd2_enabled,
                        description="Enabling creates the table and backfills the current rows; "
                        "disabling stops the updates but keeps the table (it is deleted with the form).",
                    ),
                    dmc.Code(
                        f"SELECT * FROM {scd2_path} WHERE {SCD2_END_COLUMN} IS NULL;  -- current rows",
                        block=True,
                    )
                    if form.scd2_enabled
                    else None,
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
    ]
    if can_delete:
        blocks.append(
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Title("Danger zone", order=4, c="red"),
                        dmc.Text(
                            f"Delete '{form.title}' and all of its {form.row_count or 0:,} rows. This cannot be undone. "
                            "Only global administrators can delete forms.",
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
            )
        )
    else:
        blocks.append(
            dmc.Text(
                "Deleting a form is reserved to global administrators; ask the data platform team.",
                size="sm",
                c="dimmed",
            )
        )
    return dmc.Stack(blocks, gap="md")


def _kv(d: dict[str, str]) -> str:
    return "\n".join(f"{k} = {v}" for k, v in sorted(d.items())) or "(none)"


def _import_modal(form: FormDef) -> dmc.Modal:
    keys = ", ".join(c.name for c in form.key_columns)
    return dmc.Modal(
        id=ids.IMPORT_MODAL,
        title="Import rows",
        size="xl",
        children=dmc.Stack(
            [
                dmc.Text(
                    f"Import rows into '{form.title}' from Excel or CSV. Headers are matched to column "
                    "names (spaces and punctuation are ignored).",
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
                    max_size=get_settings().max_file_mb * 1024 * 1024,
                ),
                dmc.RadioGroup(
                    id=ids.IMPORT_MODE,
                    value="append",
                    label="Import mode",
                    description=(
                        f"Merge and replace match rows on the business key ({keys})."
                        if keys
                        else "Merge and replace need business key columns; mark them on the Schema tab."
                    ),
                    children=dmc.Stack(
                        [
                            dmc.Radio(value="append", label="Append: every file row becomes a new row"),
                            dmc.Radio(
                                value="merge",
                                label="Merge: update matched rows, add new ones; other rows stay",
                                disabled=not keys,
                            ),
                            dmc.Radio(
                                value="replace",
                                label="Replace: merge, then delete the rows that are not in the file",
                                disabled=not keys,
                            ),
                        ],
                        gap=6,
                    ),
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
                            "Import",
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


# -- bulk update (FR-22) -----------------------------------------------------------------


def _bulk_columns(form: FormDef) -> list[ColumnDef]:
    return [c for c in form.user_columns if c.data_type is not DataType.OTHER]


def _bulk_modal(form: FormDef, editable: bool) -> dmc.Modal:
    columns = _bulk_columns(form)
    return dmc.Modal(
        id=ids.BULK_MODAL,
        title="Bulk update selected rows",
        size="lg",
        children=dmc.Stack(
            [
                dmc.Text(
                    "Set one column to the same value on every selected row. The change joins your unsaved "
                    "changes; nothing is written until you press Save.",
                    size="sm",
                    c="dimmed",
                ),
                dmc.Text(id=ids.BULK_INFO, size="sm", fw=600),
                dmc.Select(
                    id=ids.BULK_COLUMN,
                    label="Column",
                    data=[
                        {"value": c.name, "label": f"{humanize(c.name)} ({c.type_label})"} for c in columns
                    ],
                    value=columns[0].name if columns else None,
                    searchable=True,
                    allowDeselect=False,
                ),
                html.Div(
                    id=ids.BULK_VALUE_WRAP,
                    children=_value_control(columns[0], None, ids.BULK_VALUE, True) if columns else None,
                ),
                dmc.Checkbox(
                    id=ids.BULK_CLEAR,
                    label="Clear the value (set to empty) instead",
                    checked=False,
                ),
                dmc.Group(
                    [
                        dmc.Button(
                            "Apply to selected rows",
                            id=ids.BULK_SUBMIT,
                            leftSection=icon("tabler:replace"),
                            disabled=not editable or not columns,
                        )
                    ],
                    justify="flex-end",
                ),
            ],
            gap="sm",
        ),
    )


def _value_control(col: ColumnDef, value: Any, control_id: Any, editable: bool) -> Any:
    """A typed input for one column value (shared by the bulk-update and item forms)."""
    label = humanize(col.name)
    common: dict[str, Any] = {
        "id": control_id,
        "label": label,
        "description": col.description or None,
        "required": col.required,
        "disabled": not editable,
    }
    t = col.data_type
    if t is DataType.STRING and col.options:
        return dmc.Select(
            data=[{"value": o, "label": o} for o in col.options],
            value=value if value in col.options else None,
            searchable=True,
            clearable=True,
            placeholder="Choose a value",
            **common,
        )
    if t is DataType.BOOLEAN:
        if isinstance(value, str):
            value = value.strip().lower()
        elif value is not None:
            value = "true" if bool(value) else "false"
        return dmc.Select(
            data=BOOL_OPTIONS, value=value if value in ("true", "false") else None, clearable=True, **common
        )
    if t in (DataType.INTEGER, DataType.DECIMAL, DataType.DOUBLE):
        params: dict[str, Any] = {"hideControls": True}
        if t is DataType.INTEGER:
            params["allowDecimal"] = False
        elif t is DataType.DECIMAL:
            params["decimalScale"] = col.decimal_params[1]
        return dmc.NumberInput(value="" if value is None else value, **params, **common)
    if t is DataType.DATE:
        return dmc.DateInput(value=value or None, valueFormat="YYYY-MM-DD", clearable=True, **common)
    if t is DataType.TIMESTAMP:
        return dmc.TextInput(
            value="" if value is None else str(value), placeholder="YYYY-MM-DD HH:MM:SS", **common
        )
    if t is DataType.OTHER:
        return dmc.TextInput(value="" if value is None else str(value), **{**common, "disabled": True})
    return dmc.TextInput(value="" if value is None else str(value), **common)


# -- item form (FR-24) ---------------------------------------------------------------------


def _item_modal() -> dmc.Modal:
    return dmc.Modal(
        id=ids.ITEM_MODAL,
        title="Row",
        size="xl",
        children=dmc.Stack(
            [
                html.Div(id=ids.ITEM_BODY),
                html.Div(id=ids.ITEM_RESULT),
                dmc.Group(
                    [dmc.Button("Apply to grid", id=ids.ITEM_SAVE, leftSection=icon("tabler:check"))],
                    justify="flex-end",
                ),
            ],
            gap="sm",
        ),
    )


def _history_records(df: pd.DataFrame, form: FormDef) -> list[dict[str, Any]]:
    """History rows as JSON-safe dicts (stored in the browser for restore)."""
    return g.rows_to_records(df, form)


def item_body(form: FormDef, row: dict[str, Any], history: list[dict[str, Any]], editable: bool) -> dmc.Stack:
    """The item form: one input per column, the system columns, and the history of the row."""
    fields = [
        _value_control(c, row.get(c.name), ids.item_field_id(c.name), editable) for c in form.user_columns
    ]
    audit = [
        dmc.Text(
            f"{g.AUDIT_LABELS.get(c, humanize(c.lstrip('_')))}: {_fmt(row.get(c)) or '-'}",
            size="xs",
            c="dimmed",
        )
        for c in AUDIT_COLUMNS
        if c in row
    ]
    is_new = is_temp_id(row.get(ID_COLUMN))
    blocks: list[Any] = [
        dmc.Group(
            [
                dmc.Text(describe_row(form, row, fallback="New row" if is_new else "Row"), fw=600),
                dmc.Badge("unsaved new row", color="teal", variant="light", size="sm") if is_new else None,
            ],
            gap="sm",
        ),
        dmc.SimpleGrid(fields, cols={"base": 1, "md": 2}, spacing="sm"),
        dmc.Group(audit, gap="md") if audit and not is_new else None,
    ]
    if not editable:
        blocks.insert(1, dmc.Text("Read-only view.", size="xs", c="dimmed"))
    if not is_new:
        blocks.append(dmc.Divider(label="History of this row", labelPosition="left"))
        if not history:
            blocks.append(dmc.Text("No recorded changes for this row.", size="sm", c="dimmed"))
        else:
            head = dmc.TableThead(
                dmc.TableTr([dmc.TableTh(h) for h in ("#", "When", "By", "Change", "Fields changed", "")])
            )
            body = []
            for rec in history:
                restore = (
                    dmc.Button(
                        "Restore",
                        id=ids.restore_id(rec["version"]),
                        size="xs",
                        variant="subtle",
                        leftSection=icon("tabler:restore", 14),
                    )
                    if editable and rec.get("change_type") in ("insert", "update")
                    else None
                )
                body.append(
                    dmc.TableTr(
                        [
                            dmc.TableTd(str(rec.get("version", ""))),
                            dmc.TableTd(_fmt(rec.get("changed_at"))),
                            dmc.TableTd(_fmt(rec.get("changed_by"))),
                            dmc.TableTd(CHANGE_LABELS.get(rec.get("change_type"), rec.get("change_type"))),
                            dmc.TableTd(_fmt(rec.get("changed_fields"))),
                            dmc.TableTd(restore),
                        ]
                    )
                )
            blocks.append(
                dmc.ScrollArea(
                    dmc.Table([head, dmc.TableTbody(body)], striped=True, withTableBorder=True, fz="xs"),
                    h=220,
                    type="auto",
                )
            )
            if editable:
                blocks.append(
                    dmc.Text(
                        "Restore stages that version's values on this row; press Save on the grid to persist.",
                        size="xs",
                        c="dimmed",
                    )
                )
    return dmc.Stack([b for b in blocks if b is not None], gap="sm")


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
                        dmc.TextInput(id=ids.ADD_COL_DESC, label="Description", required=True),
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


def history_panel(ctx_: AppContext, form: FormDef, editable: bool) -> Any:
    try:
        df = ctx_.forms.history(form, limit=500)
    except BackendError as exc:
        return error_alert(exc)
    if df.empty:
        return empty_state(
            "No changes yet", "Every save is recorded here with who changed what and when.", "tabler:history"
        )
    df = df.assign(change_type=df["change_type"].map(CHANGE_LABELS).fillna(df["change_type"]))
    rows = g.rows_to_records(df, form)
    note = (
        "Backed by the audit table in the catalog (Change Data Feed as fallback)."
        if ctx_.settings.is_databricks
        else "Locally this is the _catalog.change_log table; in Databricks it is the audit table."
    )
    toolbar = [
        dmc.TextInput(
            id=ids.history_filter_id(form.name),
            placeholder="Search the history",
            leftSection=icon("tabler:search"),
            debounce=250,
            w=300,
            size="sm",
        ),
    ]
    if editable:
        toolbar.append(
            dmc.Button(
                "Restore selected version",
                id=ids.history_restore_id(form.name),
                variant="light",
                size="sm",
                leftSection=icon("tabler:restore"),
            )
        )
    return dmc.Stack(
        [
            dmc.Group(
                [
                    *toolbar,
                    dmc.Text(
                        "Edits show the row after the change; deletions show the row as it was. "
                        + note
                        + (
                            " Tick an entry and Restore to stage its values on the Data tab (a deleted row comes back as a new row)."
                            if editable
                            else ""
                        ),
                        size="xs",
                        c="dimmed",
                    ),
                ],
                gap="md",
            ),
            dag.AgGrid(
                id=ids.history_grid_id(form.name),
                rowData=rows,
                columnDefs=g.history_column_defs(form, selectable=editable),
                defaultColDef={"sortable": True, "filter": True, "resizable": True},
                dashGridOptions={
                    "rowHeight": 32,
                    "enableCellTextSelection": True,
                    "rowSelection": "single",
                    "suppressRowClickSelection": True,
                },
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
    form = ctx_.forms.get_form(key["function"], key["form"])
    role = ctx_.role_of(key["function"])
    return form, role, role.can_edit and form.is_editable


def _pattern_states(type_name: str) -> list[tuple[dict, Any]]:
    """(id, value) pairs of the pattern-matching State entries of the given ``type``."""
    out = []
    for entry in ctx.states_list:
        if isinstance(entry, list):
            for item in entry:
                if isinstance(item.get("id"), dict) and item["id"].get("type") == type_name:
                    out.append((item["id"], item.get("value")))
    return out


def _grid_row(form: FormDef, row: dict[str, Any], invalid: dict[str, list[str]]) -> dict[str, Any]:
    """A row for a ``rowTransaction`` update: values plus the invalid-cell flags."""
    out = {k: v for k, v in row.items() if not k.startswith(g.INVALID_PREFIX)}
    g.flag_invalid(out, invalid.get(str(out.get(ID_COLUMN))))
    return out


def _current_row(rows_by_id: dict[str, dict[str, Any]], draft: Draft, rid: str) -> dict[str, Any] | None:
    """The row as the user currently sees it: loaded values with the pending draft edits on top."""
    if is_temp_id(rid):
        values = draft.inserts.get(rid)
        return None if values is None else {ID_COLUMN: rid, VERSION_COLUMN: None, **values}
    base = rows_by_id.get(rid)
    if base is None:
        return None
    edits = {k: v for k, v in draft.updates.get(rid, {}).items() if k != VERSION_COLUMN}
    return {**base, **edits}


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
        running=[(Output(ids.GRID_REFRESH, "loading"), True, False)],
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
        Output(ids.PENDING, "children", allow_duplicate=True),
        Output(ids.GRID_SAVE, "disabled", allow_duplicate=True),
        Input(ids.FORM_TABS, "value"),
        State(ids.DRAFT, "data"),
        State(ids.GRID, "rowData"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def sync_save_state(tab, draft_raw, rows, key, persona):
        """Coming back to the Data tab: recompute the pending bar and Save from the draft."""
        if tab != "data" or not key:
            return no_update, no_update
        draft = Draft.from_dict(draft_raw)
        if draft.is_empty:
            return None, True
        c = get_context(persona)
        try:
            form, _role, editable = _load(c, key)
        except (BackendError, PermissionDenied):
            return no_update, no_update
        pending, save_disabled, _ = _pending_bar(form, rows or [], draft)
        return pending, save_disabled or not editable

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
        running=[(Output(ids.GRID_SAVE, "loading"), True, False)],
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
            updates = [_grid_row(form, data, invalid) for data in touched.values()]
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

    # -- bulk update, item form and restore: all land in the draft ------------------------

    @app.callback(
        Output(ids.BULK_MODAL, "opened"),
        Output(ids.BULK_INFO, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.BULK_OPEN, "n_clicks"),
        State(ids.GRID, "selectedRows"),
        prevent_initial_call=True,
    )
    def open_bulk(n, selected):
        if not n:
            return no_update, no_update, no_update
        if not selected:
            return (
                no_update,
                no_update,
                notify("Select rows first (tick the boxes in the first column).", color="yellow"),
            )
        return True, f"{len(selected):,} selected row(s) will receive the value.", no_update

    @app.callback(
        Output(ids.BULK_VALUE_WRAP, "children"),
        Input(ids.BULK_COLUMN, "value"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def bulk_value_control(column, key, persona):
        if not column or not key:
            return no_update
        c = get_context(persona)
        try:
            form, _r, editable = _load(c, key)
        except (BackendError, PermissionDenied):
            return no_update
        col = form.column(column)
        if col is None:
            return no_update
        return _value_control(col, None, ids.BULK_VALUE, editable)

    @app.callback(
        Output(ids.ITEM_MODAL, "opened"),
        Output(ids.ITEM_MODAL, "title"),
        Output(ids.ITEM_BODY, "children"),
        Output(ids.ITEM_ROW, "data"),
        Output(ids.ITEM_HISTORY, "data"),
        Output(ids.ITEM_RESULT, "children"),
        Output(ids.ITEM_SAVE, "style"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.ITEM_OPEN, "n_clicks"),
        State(ids.GRID, "selectedRows"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def open_item(n, selected, key, persona):
        if not n:
            return (no_update,) * 8
        if not selected or len(selected) != 1:
            return (no_update,) * 7 + (
                notify("Select exactly one row (tick its box in the first column).", color="yellow"),
            )
        c = get_context(persona)
        try:
            form, _r, editable = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return (no_update,) * 7 + (notify(str(exc), color="red"),)
        row = {k: v for k, v in selected[0].items() if not k.startswith(g.INVALID_PREFIX)}
        rid = str(row.get(ID_COLUMN) or "")
        history: list[dict[str, Any]] = []
        if rid and not is_temp_id(rid):
            try:
                history = _history_records(c.forms.history(form, limit=200, row_id=rid), form)
            except BackendError as exc:
                log.warning("Row history unavailable: %s", exc)
        return (
            True,
            f"{form.title}: {describe_row(form, row, fallback='row')}",
            item_body(form, row, history, editable),
            row,
            history,
            None,
            {} if editable else {"display": "none"},
            no_update,
        )

    @app.callback(
        Output(ids.DRAFT, "data", allow_duplicate=True),
        Output(ids.PENDING, "children", allow_duplicate=True),
        Output(ids.GRID, "rowTransaction", allow_duplicate=True),
        Output(ids.GRID_SAVE, "disabled", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.BULK_MODAL, "opened", allow_duplicate=True),
        Output(ids.ITEM_MODAL, "opened", allow_duplicate=True),
        Output(ids.ITEM_BODY, "children", allow_duplicate=True),
        Output(ids.ITEM_ROW, "data", allow_duplicate=True),
        Output(ids.ITEM_RESULT, "children", allow_duplicate=True),
        Output(ids.GRID, "deselectAll", allow_duplicate=True),
        Input(ids.BULK_SUBMIT, "n_clicks"),
        Input(ids.ITEM_SAVE, "n_clicks"),
        Input({"type": "restore", "version": ALL}, "n_clicks"),
        Input({"type": "history-restore", "form": ALL}, "n_clicks"),
        State(ids.DRAFT, "data"),
        State(ids.GRID, "rowData"),
        State(ids.GRID, "selectedRows"),
        State(ids.BULK_COLUMN, "value"),
        State(ids.BULK_VALUE, "value"),
        State(ids.BULK_CLEAR, "checked"),
        State({"type": "item-field", "column": ALL}, "value"),
        State(ids.ITEM_ROW, "data"),
        State(ids.ITEM_HISTORY, "data"),
        State({"type": "history-grid", "form": ALL}, "selectedRows"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
        running=[
            (Output(ids.BULK_SUBMIT, "loading"), True, False),
            (Output(ids.ITEM_SAVE, "loading"), True, False),
        ],
    )
    def draft_actions(
        n_bulk,
        n_item,
        _n_restore,
        _n_history_restore,
        draft_raw,
        rows,
        selected,
        bulk_column,
        bulk_raw,
        bulk_clear,
        _item_values,
        item_row,
        item_history,
        history_selected,
        key,
        persona,
    ):
        trigger = ctx.triggered_id
        triggered_value = ctx.triggered[0]["value"] if ctx.triggered else None
        if trigger is None or not triggered_value:
            return (no_update,) * 11  # mounted, not clicked
        c = get_context(persona)
        try:
            form, _role, editable = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return (no_update,) * 4 + (notify(str(exc), color="red"),) + (no_update,) * 6
        if not editable:
            return (no_update,) * 4 + (notify("You cannot edit this form.", color="red"),) + (no_update,) * 6
        draft = Draft.from_dict(draft_raw)
        rows_by_id = {str(r.get(ID_COLUMN)): r for r in (rows or [])}
        out: dict[str, Any] = {}

        def finish(transaction: Any, message: str | None = None, color: str = "teal") -> tuple:
            pending, save_disabled, invalid = _pending_bar(form, rows or [], draft)
            if isinstance(transaction, dict):
                for kind in ("update", "add"):
                    if kind in transaction:
                        transaction[kind] = [_grid_row(form, r, invalid) for r in transaction[kind]]
            return (
                draft.to_dict(),
                pending,
                transaction,
                save_disabled,
                notify(message)
                if message and color == "teal"
                else (notify(message, color=color) if message else no_update),
                out.get("bulk_opened", no_update),
                out.get("item_opened", no_update),
                out.get("item_body", no_update),
                out.get("item_row", no_update),
                out.get("item_result", no_update),
                out.get("deselect", no_update),
            )

        # -- bulk update of the selected rows --------------------------------------------
        if trigger == ids.BULK_SUBMIT:
            if not selected:
                return (
                    (no_update,) * 4 + (notify("No rows are selected.", color="yellow"),) + (no_update,) * 6
                )
            if not bulk_column:
                return (no_update,) * 4 + (notify("Choose a column.", color="yellow"),) + (no_update,) * 6
            try:
                value = bulk_value(form, bulk_column, None if bulk_clear else bulk_raw)
            except ValueError as exc:
                return (
                    (no_update,) * 4
                    + (notify(str(exc), title="Not applied", color="red"),)
                    + (no_update,) * 6
                )
            touched = draft.set_many(selected, bulk_column, value)
            updates = [{**r, bulk_column: value} for r in selected if str(r.get(ID_COLUMN)) in touched]
            out["bulk_opened"] = False
            out["deselect"] = True
            return finish(
                {"update": updates}, f"{len(touched):,} row(s) updated in the draft; press Save to persist."
            )

        # -- item form: apply the field values to the row --------------------------------
        if trigger == ids.ITEM_SAVE:
            if not item_row:
                return (no_update,) * 4 + (notify("Open a row first.", color="yellow"),) + (no_update,) * 6
            rid = str(item_row.get(ID_COLUMN))
            merged = dict(item_row)
            changed = 0
            for field_id, raw in _pattern_states("item-field"):
                col = form.column(field_id["column"])
                if col is None or col.data_type is DataType.OTHER:
                    continue
                if isinstance(raw, str) and raw.strip() == "":
                    raw = None
                try:
                    value = json_safe(coerce_value(col, raw))
                except CoercionError:
                    value = raw  # invalid: keep it so the validation highlights the problem
                if not same_value(col, item_row.get(col.name), value):
                    draft.set_cell(rid, col.name, value, item_row.get(VERSION_COLUMN))
                    merged[col.name] = value
                    changed += 1
            out["item_opened"] = False
            if not changed:
                return finish(no_update, "No field changed.", "gray")
            return finish(
                {"update": [merged]}, f"{changed} field(s) updated in the draft; press Save to persist."
            )

        # -- restore a version from the item form ----------------------------------------
        if isinstance(trigger, dict) and trigger.get("type") == "restore":
            record = next(
                (r for r in (item_history or []) if int(r.get("version", -1)) == int(trigger["version"])),
                None,
            )
            if not item_row or record is None:
                return (
                    (no_update,) * 4
                    + (notify("That version is no longer available.", color="yellow"),)
                    + (no_update,) * 6
                )
            rid, changed = restore_row(draft, form, item_row, record)
            merged = {
                **item_row,
                **{k: v for k, v in draft.updates.get(rid, {}).items() if k != VERSION_COLUMN},
            }
            if is_temp_id(rid):
                merged = {**item_row, **draft.inserts.get(rid, {})}
            out["item_row"] = merged
            out["item_body"] = item_body(form, merged, item_history or [], editable)
            out["item_result"] = info_alert(
                f"Version {trigger['version']} restored into the draft ({changed} field(s) changed). "
                "Close this dialog and press Save to persist.",
                color="teal",
            )
            return finish({"update": [merged]} if changed else no_update)

        # -- restore the entry selected on the History tab -------------------------------
        if isinstance(trigger, dict) and trigger.get("type") == "history-restore":
            picked = next((s[0] for s in (history_selected or []) if s), None)
            if not picked:
                return (
                    (no_update,) * 4
                    + (notify("Tick a history entry first.", color="yellow"),)
                    + (no_update,) * 6
                )
            rid = str(picked.get(ID_COLUMN) or "")
            current = _current_row(rows_by_id, draft, rid) if rid else None
            if rid in draft.deletes:  # undo a pending delete, then apply the version's values
                draft.deletes.pop(rid, None)
                base = rows_by_id.get(rid, {})
                new_id, changed = restore_row(draft, form, base, picked)
                merged = {
                    **base,
                    **{k: v for k, v in draft.updates.get(rid, {}).items() if k != VERSION_COLUMN},
                }
                return finish(
                    {"add": [merged], "addIndex": 0}, "Row restored into the draft; press Save to persist."
                )
            new_id, changed = restore_row(draft, form, current, picked)
            if is_temp_id(new_id):
                row = {c_.name: None for c_ in form.columns}
                row.update(draft.inserts.get(new_id, {}))
                row[ID_COLUMN] = new_id
                row[VERSION_COLUMN] = None
                row[g.NEW_FLAG] = True
                return finish(
                    {"add": [row], "addIndex": 0},
                    "The deleted row is staged as a new row on the Data tab; press Save to persist.",
                )
            merged = {
                **(current or {}),
                **{k: v for k, v in draft.updates.get(new_id, {}).items() if k != VERSION_COLUMN},
            }
            return finish(
                {"update": [merged]} if changed else no_update,
                f"Version restored into the draft ({changed} field(s) changed); press Save on the Data tab.",
            )
        return (no_update,) * 11

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
            form, role, editable = _load(c, key)
        except (BackendError, PermissionDenied) as exc:
            return error_alert(exc), error_alert(exc)
        if tab == "history":
            return history_panel(c, form, editable), no_update
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
        running=[(Output(ids.SCHEMA_SAVE, "loading"), True, False)],
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
                if not col.description:
                    problems.append(f"'{col.name}': a description is required.")
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
        running=[(Output(ids.ADD_COL_SUBMIT, "loading"), True, False)],
    )
    def add_column(n, raw_name, type_value, desc, precision, scale, key, pathname, persona):
        if not n:
            return no_update, no_update, no_update
        if not (raw_name or "").strip():
            return no_update, notify("A column name is required", color="red"), no_update
        if not (desc or "").strip():
            return no_update, notify("A column description is required", color="red"), no_update
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
        running=[(Output(ids.DROP_COL_SUBMIT, "loading"), True, False)],
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
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Input(ids.SCD2_SWITCH, "checked"),
        State(ids.FORM_KEY, "data"),
        State(ids.URL, "pathname"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
        running=[(Output(ids.SCD2_SWITCH, "disabled"), True, False)],
    )
    def toggle_scd2(checked, key, pathname, persona):
        c = get_context(persona)
        try:
            form, _r, _e = _load(c, key)
            if bool(checked) == form.scd2_enabled:
                return no_update, no_update
            c.forms.set_scd2(form, bool(checked))
        except (BackendError, PermissionDenied, ValueError) as exc:
            return notify(str(exc), title="Not changed", color="red"), no_update
        invalidate_metadata()
        message = "History table enabled and backfilled" if checked else "History table disabled"
        return notify(message), pathname  # re-render the page to show or hide the query snippet

    @app.callback(
        Output(ids.SETTINGS_RESULT, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.SETTINGS_SAVE, "n_clicks"),
        State(ids.SETTINGS_DISPLAY, "value"),
        State(ids.SETTINGS_DESC, "value"),
        State(ids.SETTINGS_OWNER, "value"),
        State(ids.SETTINGS_OWNER_EMAIL, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
        running=[(Output(ids.SETTINGS_SAVE, "loading"), True, False)],
    )
    def save_settings(n, display, desc, owner, owner_email, nav_version, key, persona):
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
            updated.owner_email = (owner_email or "").strip()
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
        running=[(Output(ids.DROP_FORM_SUBMIT, "loading"), True, False)],
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
            function_href(key["function"]),
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
        Input(ids.IMPORT_MODE, "value"),
        State(ids.IMPORT_UPLOAD, "filename"),
        State(ids.IMPORT_TOKEN, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def preview_import(contents, sheet, mode, filename, token, key, persona):
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
        if mode in ("merge", "replace"):
            try:
                current = c.forms.load_rows(form, limit=(form.row_count or 0) + 1)
                changes, plan_problems = build_import_changeset(form, current, frame, mode)
            except (BackendError, PermissionDenied, ValueError) as exc:
                return error_alert(exc, "Cannot plan the import"), token, sheet_data, sheet, sheet_style, True
            if plan_problems:
                blocks.append(
                    dmc.Alert(
                        dmc.List([dmc.ListItem(p) for p in plan_problems[:20]], size="sm"),
                        title="The file cannot be merged",
                        color="red",
                        variant="light",
                    )
                )
                return dmc.Stack(blocks, gap="xs"), token, sheet_data, sheet, sheet_style, True
            unchanged = len(frame) - len(changes.inserts) - len(changes.updates)
            plan = (
                f"Plan: {len(changes.inserts):,} new, {len(changes.updates):,} updated, "
                f"{unchanged:,} unchanged"
            )
            if mode == "replace":
                plan += f", {len(changes.deletes):,} deleted"
            blocks.append(dmc.Text(plan, size="sm", fw=600))
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
        State(ids.IMPORT_MODE, "value"),
        State(ids.GRID_VERSION, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.FORM_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
        running=[(Output(ids.IMPORT_SUBMIT, "loading"), True, False)],
    )
    def submit_import(n, token, sheet, mode, version, nav_version, key, persona):
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
            if mode in ("merge", "replace"):
                current = c.forms.load_rows(form, limit=(form.row_count or 0) + 1)
                changes, problems = build_import_changeset(form, current, frame, mode)
                if problems:
                    return (
                        no_update,
                        no_update,
                        notify(" ".join(problems[:5]), title="Import failed", color="red"),
                        no_update,
                    )
                result = c.forms.save(form, changes)
                message = f"Import applied: {result.summary()}" if result.applied else "Nothing changed"
                if result.conflicts:
                    message += f" \u00b7 {len(result.conflicts)} row(s) changed meanwhile and were skipped"
            else:
                count = c.forms.append_rows(form, frame)
                message = f"Imported {count:,} rows"
        except (ImportError_, BackendError, PermissionDenied, ValueError) as exc:
            return no_update, no_update, notify(str(exc), title="Import failed", color="red"), no_update
        uploads.drop(token)
        invalidate_metadata()
        return False, (version or 0) + 1, notify(message), (nav_version or 0) + 1

    @app.callback(
        Output({"type": "history-grid", "form": MATCH}, "dashGridOptions"),
        Input({"type": "history-filter", "form": MATCH}, "value"),
        prevent_initial_call=True,
    )
    def filter_history(text):
        return {
            "rowHeight": 32,
            "enableCellTextSelection": True,
            "rowSelection": "single",
            "suppressRowClickSelection": True,
            "quickFilterText": text or "",
        }
