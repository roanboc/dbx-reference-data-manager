"""Administrator wizard: create a form from an Excel/CSV file or from scratch."""

from __future__ import annotations

import logging
from typing import Any

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import Input, Output, State, ctx, dcc, html, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import ColumnDef, DataType, FormDef, humanize, sanitize_identifier
from rdm.services.excel_import import ImportError_, ParsedSheet, coerce_frame, list_sheets, parse_file
from rdm.ui import grid as g
from rdm.ui import ids, uploads
from rdm.ui.components import error_alert, icon, info_alert, issues_list, notify, page_title
from rdm.ui.context import AppContext, get_context, invalidate_metadata, navigation
from rdm.ui.layout import form_href

log = logging.getLogger(__name__)
GRID_THEME = "ag-theme-quartz"
STEPS = [
    ("Source", "Upload a file or start from scratch"),
    ("Columns", "Confirm names, types and rules"),
    ("Details", "Function, name and description"),
    ("Review", "Check and create"),
]
TYPE_VALUES = [t.value for t in DataType.editable_types()]


def _default_state(function: str | None = None) -> dict[str, Any]:
    return {
        "step": 0,
        "mode": "upload",
        "token": None,
        "file_name": "",
        "sheet": None,
        "header_row": 1,
        "columns": [],
        "function": function or "",
        "name": "",
        "display_name": "",
        "description": "",
        "owner": "",
        "load_rows": True,
    }


def render(ctx_: AppContext, function: str | None = None) -> dmc.Stack:
    """The wizard; ``function`` (from ``/new-form/<function>``) pre-selects the function when the
    user administers it."""
    header = page_title(
        "New form",
        "Create a governed, editable list from an Excel file or from a hand-written column definition.",
    )
    if not ctx_.permissions.admin_functions:
        return dmc.Stack(
            [
                header,
                info_alert(
                    "You need Function admin access to at least one function to create forms.",
                    "Function admins only",
                    "yellow",
                ),
            ]
        )
    return dmc.Stack(
        [
            dcc.Store(
                id=ids.WIZ_STORE,
                data=_default_state(function if function in ctx_.permissions.admin_functions else None),
            ),
            header,
            dmc.Stepper(
                id=ids.WIZ_STEPPER,
                active=0,
                children=[dmc.StepperStep(label=label, description=desc) for label, desc in STEPS],
                size="sm",
            ),
            html.Div(id=ids.WIZ_BODY),
        ],
        gap="md",
    )


def _stubs(state: dict[str, Any], present: set[str]) -> html.Div:
    """Hidden placeholders for the wizard controls a step does not show.

    Dash resolves every Input/State of a callback against the current layout, so each step
    renders the real controls it needs and invisible stand-ins for the rest.
    """
    factories = {
        ids.WIZ_MODE: lambda: dmc.SegmentedControl(
            id=ids.WIZ_MODE,
            value=state["mode"],
            data=[{"value": "upload", "label": "u"}, {"value": "scratch", "label": "s"}],
        ),
        ids.WIZ_UPLOAD: lambda: dcc.Upload(id=ids.WIZ_UPLOAD, children=""),
        ids.WIZ_SHEET: lambda: dmc.Select(id=ids.WIZ_SHEET, data=[], value=None),
        ids.WIZ_HEADER_ROW: lambda: dmc.NumberInput(
            id=ids.WIZ_HEADER_ROW, value=int(state.get("header_row") or 1)
        ),
        ids.WIZ_BACK: lambda: dmc.Button("", id=ids.WIZ_BACK),
        ids.WIZ_NEXT: lambda: dmc.Button("", id=ids.WIZ_NEXT),
        ids.WIZ_CANCEL: lambda: dmc.Button("", id=ids.WIZ_CANCEL),
        ids.WIZ_CREATE: lambda: dmc.Button("", id=ids.WIZ_CREATE),
        ids.WIZ_ADD_COLUMN: lambda: dmc.Button("", id=ids.WIZ_ADD_COLUMN),
        ids.WIZ_COLUMNS_GRID: lambda: dag.AgGrid(id=ids.WIZ_COLUMNS_GRID, rowData=[], columnDefs=[]),
        ids.WIZ_FUNCTION: lambda: dmc.Select(
            id=ids.WIZ_FUNCTION, data=[], value=state.get("function") or None
        ),
        ids.WIZ_NAME: lambda: dmc.TextInput(id=ids.WIZ_NAME, value=state.get("name") or ""),
        ids.WIZ_DISPLAY: lambda: dmc.TextInput(id=ids.WIZ_DISPLAY, value=state.get("display_name") or ""),
        ids.WIZ_DESC: lambda: dmc.Textarea(id=ids.WIZ_DESC, value=state.get("description") or ""),
        ids.WIZ_OWNER: lambda: dmc.TextInput(id=ids.WIZ_OWNER, value=state.get("owner") or ""),
        ids.WIZ_LOAD_ROWS: lambda: dmc.Checkbox(id=ids.WIZ_LOAD_ROWS, checked=bool(state.get("load_rows"))),
        ids.WIZ_ERRORS: lambda: html.Div(id=ids.WIZ_ERRORS),
    }
    return html.Div(
        [make() for key, make in factories.items() if key not in present], style={"display": "none"}
    )


# --------------------------------------------------------------------------------------
# Step bodies
# --------------------------------------------------------------------------------------


def _parsed(state: dict[str, Any]) -> ParsedSheet | None:
    stored = uploads.get(state.get("token"))
    if stored is None:
        return None
    name, data = stored
    sheet = state.get("sheet")
    return parse_file(
        data,
        name,
        sheet=None if sheet in (None, "data") else sheet,
        header_row=int(state.get("header_row") or 1) - 1,
    )


def _rows_from_parsed(parsed: ParsedSheet) -> list[dict[str, Any]]:
    return [
        {
            "name": c.name,
            "type": c.data_type.value,
            "description": "",
            "required": False,
            "key": c.name in parsed.suggested_keys,
            "options": ", ".join(parsed.suggested_options.get(c.name, [])),
            "samples": ", ".join(parsed.samples.get(c.name, [])),
            "source": parsed.source_names.get(c.name, ""),
        }
        for c in parsed.columns
    ]


def _nav_buttons(back: bool = True, next_label: str = "Next", next_id: str = ids.WIZ_NEXT) -> dmc.Group:
    buttons = []
    if back:
        buttons.append(
            dmc.Button("Back", id=ids.WIZ_BACK, variant="default", leftSection=icon("tabler:arrow-left"))
        )
    buttons.append(dmc.Button("Cancel", id=ids.WIZ_CANCEL, variant="subtle", color="gray"))
    buttons.append(
        dmc.Button(
            next_label,
            id=next_id,
            rightSection=icon("tabler:arrow-right")
            if next_id == ids.WIZ_NEXT
            else icon("tabler:circle-plus"),
        )
    )
    return dmc.Group(buttons, justify="flex-end", mt="md")


def step_source(state: dict[str, Any]) -> dmc.Stack:
    stored = uploads.get(state.get("token"))
    preview: Any = None
    sheet_select = dmc.Select(id=ids.WIZ_SHEET, label="Sheet", data=[], value=None, style={"display": "none"})
    if stored is not None and state["mode"] == "upload":
        name, data = stored
        try:
            sheets = list_sheets(data, name)
            parsed = _parsed(state)
        except (ImportError_, ValueError) as exc:
            preview = error_alert(exc, "Cannot read the file")
            parsed = None
            sheets = []
        if parsed is not None:
            sheet_select = dmc.Select(
                id=ids.WIZ_SHEET,
                label="Sheet",
                data=[{"value": s, "label": s} for s in sheets],
                value=state.get("sheet") or sheets[0],
                style={} if len(sheets) > 1 else {"display": "none"},
                w=240,
            )
            preview = dmc.Stack(
                [
                    dmc.Text(
                        f"{name} · {parsed.row_count:,} data rows · {len(parsed.columns)} columns detected",
                        size="sm",
                        fw=500,
                    ),
                    dag.AgGrid(
                        rowData=[
                            {str(k): g.to_json_value(v) for k, v in r.items()}
                            for r in parsed.raw.head(10).to_dict("records")
                        ],
                        columnDefs=[{"field": str(c), "headerName": str(c)} for c in parsed.raw.columns],
                        defaultColDef={"resizable": True},
                        columnSize="autoSize",
                        className=GRID_THEME,
                        style={"height": "280px"},
                    ),
                    *[dmc.Alert(w, color="yellow", variant="light") for w in parsed.warnings],
                ],
                gap="xs",
            )
    mode = dmc.SegmentedControl(
        id=ids.WIZ_MODE,
        value=state["mode"],
        data=[
            {"value": "upload", "label": "Upload an Excel or CSV file"},
            {"value": "scratch", "label": "Start from scratch"},
        ],
    )
    upload_block = dmc.Stack(
        [
            dcc.Upload(
                id=ids.WIZ_UPLOAD,
                children=html.Div(
                    [
                        "Drag and drop or ",
                        html.B("click to choose"),
                        " an Excel or CSV file. The header row becomes the column names; types are inferred.",
                    ]
                ),
                className="rdm-dropzone",
                multiple=False,
                accept=".xlsx,.xls,.csv,.tsv",
            ),
            dmc.Group(
                [
                    sheet_select,
                    dmc.NumberInput(
                        id=ids.WIZ_HEADER_ROW,
                        label="Header row (1 = first)",
                        value=int(state.get("header_row") or 1),
                        min=1,
                        max=50,
                        w=200,
                    ),
                ]
            ),
            preview,
        ],
        gap="sm",
        style={} if state["mode"] == "upload" else {"display": "none"},
    )
    scratch_block = (
        info_alert("You will define the columns by hand in the next step.", color="gray")
        if state["mode"] == "scratch"
        else None
    )
    return dmc.Stack(
        [
            dmc.Title("Where does the data come from?", order=4),
            mode,
            upload_block,
            scratch_block,
            _nav_buttons(back=False),
            _stubs(
                state,
                {
                    ids.WIZ_MODE,
                    ids.WIZ_UPLOAD,
                    ids.WIZ_SHEET,
                    ids.WIZ_HEADER_ROW,
                    ids.WIZ_NEXT,
                    ids.WIZ_CANCEL,
                },
            ),
        ],
        gap="sm",
    )


def step_columns(state: dict[str, Any]) -> dmc.Stack:
    rows = state.get("columns") or []
    has_file = state.get("mode") == "upload" and uploads.get(state.get("token")) is not None
    defs = [
        {
            "field": "name",
            "headerName": "Column name",
            "editable": True,
            "minWidth": 170,
            "pinned": "left",
            "headerTooltip": "Letters, digits and underscores",
        },
        {
            "field": "type",
            "headerName": "Type",
            "editable": True,
            "cellEditor": "agSelectCellEditor",
            "cellEditorParams": {"values": TYPE_VALUES},
            "maxWidth": 140,
        },
        {"field": "description", "headerName": "Description", "editable": True, "minWidth": 220, "flex": 2},
        {
            "field": "required",
            "headerName": "Required",
            "editable": True,
            "cellRenderer": "agCheckboxCellRenderer",
            "cellEditor": "agCheckboxCellEditor",
            "maxWidth": 110,
        },
        {
            "field": "key",
            "headerName": "Business key",
            "editable": True,
            "cellRenderer": "agCheckboxCellRenderer",
            "cellEditor": "agCheckboxCellEditor",
            "maxWidth": 130,
        },
        {
            "field": "options",
            "headerName": "Allowed values",
            "editable": True,
            "minWidth": 200,
            "headerTooltip": "Comma-separated; text columns only",
        },
        {
            "field": "samples",
            "headerName": "Sample values",
            "editable": False,
            "minWidth": 220,
            "cellClass": "rdm-audit",
            "hide": not has_file,
            "headerTooltip": "First values found in the file (read-only)",
        },
        {"field": "source", "hide": True},
    ]
    legend = ", ".join(f"{t.value} = {t.label}" for t in DataType.editable_types())
    return dmc.Stack(
        [
            dmc.Title("Confirm the columns", order=4),
            dmc.Alert(
                dmc.Stack(
                    [
                        dmc.Text(
                            "Double-click a cell to edit it. Each column of the table below defines one column of your form:",
                            size="sm",
                        ),
                        dmc.List(
                            [
                                dmc.ListItem(
                                    dmc.Text(
                                        [
                                            dmc.Text("Column name", fw=600, span=True),
                                            " - technical name in lower_snake_case (the grid shows it as 'Cost Centre Code').",
                                        ],
                                        size="sm",
                                    )
                                ),
                                dmc.ListItem(
                                    dmc.Text(
                                        [
                                            dmc.Text("Type", fw=600, span=True),
                                            " - "
                                            + legend
                                            + ". Types cannot be changed once the form exists.",
                                        ],
                                        size="sm",
                                    )
                                ),
                                dmc.ListItem(
                                    dmc.Text(
                                        [
                                            dmc.Text("Required", fw=600, span=True),
                                            " - the value can never be empty. ",
                                            dmc.Text("Business key", fw=600, span=True),
                                            " - the column(s) that identify a row; duplicates are refused.",
                                        ],
                                        size="sm",
                                    )
                                ),
                                dmc.ListItem(
                                    dmc.Text(
                                        [
                                            dmc.Text("Allowed values", fw=600, span=True),
                                            " - type a comma-separated list such as ",
                                            dmc.Code("Active, Inactive, Retired"),
                                            " to turn a text column into a dropdown; leave it empty for free text.",
                                            " Suggestions are pre-filled for text columns with few distinct values in your file.",
                                        ],
                                        size="sm",
                                    )
                                ),
                                dmc.ListItem(
                                    dmc.Text(
                                        [
                                            dmc.Text("Sample values", fw=600, span=True),
                                            " - read-only preview of the first values in your file, so you can check the inferred type.",
                                        ],
                                        size="sm",
                                    )
                                )
                                if has_file
                                else None,
                            ],
                            size="sm",
                            spacing=2,
                        ),
                    ],
                    gap=4,
                ),
                color="blue",
                variant="light",
                icon=icon("tabler:info-circle"),
            ),
            dag.AgGrid(
                id=ids.WIZ_COLUMNS_GRID,
                rowData=rows,
                columnDefs=defs,
                getRowId="params.data.name + '|' + params.data.source",
                defaultColDef={"resizable": True, "sortable": False},
                dashGridOptions={
                    "rowHeight": 34,
                    "stopEditingWhenCellsLoseFocus": True,
                    "singleClickEdit": True,
                    "rowSelection": "multiple",
                    "suppressRowClickSelection": False,
                },
                columnSize="responsiveSizeToFit",
                className=GRID_THEME,
                style={"height": f"{min(140 + 34 * max(len(rows), 3), 520)}px"},
            ),
            dmc.Group(
                [
                    dmc.Button(
                        "Add column",
                        id=ids.WIZ_ADD_COLUMN,
                        variant="light",
                        size="xs",
                        leftSection=icon("tabler:plus"),
                    )
                ]
            ),
            html.Div(id=ids.WIZ_ERRORS),
            _nav_buttons(),
            _stubs(
                state,
                {
                    ids.WIZ_COLUMNS_GRID,
                    ids.WIZ_ADD_COLUMN,
                    ids.WIZ_ERRORS,
                    ids.WIZ_BACK,
                    ids.WIZ_NEXT,
                    ids.WIZ_CANCEL,
                },
            ),
        ],
        gap="sm",
    )


def step_details(ctx_: AppContext, state: dict[str, Any]) -> dmc.Stack:
    titles = {item.function.name: item.function.title for item in navigation(ctx_, None)}
    functions = ctx_.permissions.admin_functions
    has_file = uploads.get(state.get("token")) is not None and state["mode"] == "upload"
    return dmc.Stack(
        [
            dmc.Title("Describe the form", order=4),
            dmc.SimpleGrid(
                [
                    dmc.Stack(
                        [
                            dmc.Select(
                                id=ids.WIZ_FUNCTION,
                                label="Function",
                                data=[
                                    {"value": f, "label": f"{titles.get(f, humanize(f))} ({f})"}
                                    for f in functions
                                ],
                                value=state.get("function") or (functions[0] if functions else None),
                                allowDeselect=False,
                                description="You can only create forms in functions you administer",
                                searchable=True,
                            ),
                            dmc.TextInput(
                                id=ids.WIZ_NAME,
                                label="Table name",
                                value=state.get("name") or "",
                                description="lower_snake_case; becomes the Unity Catalog table name",
                                required=True,
                            ),
                            dmc.TextInput(
                                id=ids.WIZ_DISPLAY,
                                label="Display name",
                                value=state.get("display_name") or "",
                            ),
                        ],
                        gap="sm",
                    ),
                    dmc.Stack(
                        [
                            dmc.Textarea(
                                id=ids.WIZ_DESC,
                                label="Description",
                                value=state.get("description") or "",
                                description="Stored as the table comment; searchable in the sidebar",
                                autosize=True,
                                minRows=3,
                            ),
                            dmc.TextInput(
                                id=ids.WIZ_OWNER,
                                label="Owner",
                                value=state.get("owner") or ctx_.user.email or ctx_.user.username,
                            ),
                            dmc.Checkbox(
                                id=ids.WIZ_LOAD_ROWS,
                                label="Load the rows from the file",
                                checked=bool(state.get("load_rows")) and has_file,
                                style={} if has_file else {"display": "none"},
                            ),
                        ],
                        gap="sm",
                    ),
                ],
                cols={"base": 1, "md": 2},
            ),
            html.Div(id=ids.WIZ_ERRORS),
            _nav_buttons(),
            _stubs(
                state,
                {
                    ids.WIZ_FUNCTION,
                    ids.WIZ_NAME,
                    ids.WIZ_DISPLAY,
                    ids.WIZ_DESC,
                    ids.WIZ_OWNER,
                    ids.WIZ_LOAD_ROWS,
                    ids.WIZ_ERRORS,
                    ids.WIZ_BACK,
                    ids.WIZ_NEXT,
                    ids.WIZ_CANCEL,
                },
            ),
        ],
        gap="sm",
    )


def build_columns(rows: list[dict[str, Any]]) -> tuple[list[ColumnDef], list[str]]:
    columns: list[ColumnDef] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        raw_name = (r.get("name") or "").strip()
        if not raw_name:
            errors.append(f"Row {i + 1}: a column name is required.")
            continue
        name = sanitize_identifier(raw_name)
        if name in seen:
            errors.append(f"Duplicate column name '{name}'.")
            continue
        seen.add(name)
        try:
            dtype = DataType(r.get("type") or "STRING")
        except ValueError:
            errors.append(f"'{name}': unknown type {r.get('type')!r}.")
            continue
        raw_opts = (r.get("options") or "").strip()
        opts = [o.strip() for o in raw_opts.split(",") if o.strip()] if raw_opts else []
        if opts and dtype is not DataType.STRING:
            errors.append(f"'{name}': allowed values are only supported for text columns.")
            continue
        col = ColumnDef(
            name=name,
            data_type=dtype,
            description=(r.get("description") or "").strip(),
            nullable=not bool(r.get("required")),
            options=list(dict.fromkeys(opts)),
            is_key=bool(r.get("key")),
            position=i,
        )
        try:
            col.validate()
        except ValueError as exc:
            errors.append(str(exc))
            continue
        columns.append(col)
    if not columns and not errors:
        errors.append("Define at least one column.")
    return columns, errors


def assemble(state: dict[str, Any]) -> tuple[FormDef, list[ColumnDef], list[str], dict[str, str]]:
    columns, errors = build_columns(state.get("columns") or [])
    mapping: dict[str, str] = {}
    for row, col in zip(
        [r for r in state.get("columns") or [] if (r.get("name") or "").strip()], columns, strict=False
    ):
        src = row.get("source") or ""
        if src:
            mapping[col.name] = src
    form = FormDef(
        function=state.get("function") or "",
        name=state.get("name") or "",
        display_name=(state.get("display_name") or "").strip(),
        description=(state.get("description") or "").strip(),
        owner=(state.get("owner") or "").strip(),
        columns=columns,
    )
    return form, columns, errors, mapping


def step_review(state: dict[str, Any]) -> dmc.Stack:
    form, columns, errors, mapping = assemble(state)
    blocks: list[Any] = [dmc.Title("Review and create", order=4)]
    if errors:
        blocks.append(
            dmc.Alert(
                dmc.List([dmc.ListItem(e) for e in errors], size="sm"),
                color="red",
                variant="light",
                title="Fix these first",
            )
        )
    blocks.append(
        dmc.SimpleGrid(
            [
                dmc.Stack(
                    [
                        dmc.Text(form.title, fw=600, size="lg"),
                        dmc.Code(f"{form.function}.{form.name}"),
                        dmc.Text(form.description or "No description", size="sm", c="dimmed"),
                        dmc.Text(f"Owner: {form.owner or '-'}", size="sm", c="dimmed"),
                    ],
                    gap=4,
                ),
                dmc.Text(
                    f"{len(columns)} columns (+ system columns _id, _version, _created_*, _updated_*)",
                    size="sm",
                ),
            ],
            cols={"base": 1, "md": 2},
        )
    )
    blocks.append(
        dag.AgGrid(
            rowData=[
                {
                    "column": c.name,
                    "type": c.type_label,
                    "required": c.required,
                    "key": c.is_key,
                    "allowed": ", ".join(c.options),
                    "description": c.description,
                }
                for c in columns
            ],
            columnDefs=[
                {"field": "column", "headerName": "Column"},
                {"field": "type", "headerName": "Type"},
                {"field": "required", "headerName": "Required", "cellRenderer": "agCheckboxCellRenderer"},
                {"field": "key", "headerName": "Key", "cellRenderer": "agCheckboxCellRenderer"},
                {"field": "allowed", "headerName": "Allowed values"},
                {"field": "description", "headerName": "Description", "flex": 2},
            ],
            defaultColDef={"resizable": True, "editable": False},
            columnSize="responsiveSizeToFit",
            className=GRID_THEME,
            style={"height": f"{min(120 + 34 * max(len(columns), 2), 400)}px"},
        )
    )
    parsed = _parsed(state) if state["mode"] == "upload" and state.get("load_rows") else None
    if parsed is not None and not errors:
        frame, issues = coerce_frame(parsed.raw, columns, mapping)
        blocks.append(dmc.Text(f"Data: {len(frame):,} rows will be loaded.", fw=500, size="sm"))
        if issues:
            blocks.append(
                dmc.Alert(
                    dmc.Stack(
                        [
                            dmc.Text(
                                f"{len(issues)} cell(s) do not match the chosen types and will be left empty (or go back and change the types):",
                                size="sm",
                            ),
                            issues_list(issues),
                        ],
                        gap=4,
                    ),
                    color="yellow",
                    variant="light",
                )
            )
        blocks.append(
            dag.AgGrid(
                rowData=g.rows_to_records(frame.head(10), form),
                columnDefs=[{"field": c.name, "headerName": humanize(c.name)} for c in columns],
                defaultColDef={"resizable": True},
                columnSize="autoSize",
                className=GRID_THEME,
                style={"height": "260px"},
            )
        )
    else:
        blocks.append(dmc.Text("Data: the form starts empty.", size="sm"))
    blocks.append(html.Div(id=ids.WIZ_ERRORS))
    blocks.append(_nav_buttons(next_label="Create form", next_id=ids.WIZ_CREATE))
    blocks.append(_stubs(state, {ids.WIZ_ERRORS, ids.WIZ_BACK, ids.WIZ_CREATE, ids.WIZ_CANCEL}))
    return dmc.Stack(blocks, gap="sm")


# --------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------


def register(app) -> None:
    @app.callback(
        Output(ids.WIZ_BODY, "children"),
        Output(ids.WIZ_STEPPER, "active"),
        Input(ids.WIZ_STORE, "data"),
        State(ids.PERSONA, "data"),
    )
    def render_step(state, persona):
        state = state or _default_state()
        step = int(state.get("step", 0))
        try:
            if step == 0:
                body = step_source(state)
            elif step == 1:
                body = step_columns(state)
            elif step == 2:
                body = step_details(get_context(persona), state)
            else:
                body = step_review(state)
        except Exception as exc:  # noqa: BLE001
            log.exception("wizard step failed")
            body = error_alert(exc)
        return body, step

    @app.callback(
        Output(ids.WIZ_STORE, "data", allow_duplicate=True),
        Input(ids.WIZ_MODE, "value"),
        Input(ids.WIZ_UPLOAD, "contents"),
        Input(ids.WIZ_SHEET, "value"),
        Input(ids.WIZ_HEADER_ROW, "value"),
        State(ids.WIZ_UPLOAD, "filename"),
        State(ids.WIZ_STORE, "data"),
        prevent_initial_call=True,
    )
    def source_changed(mode, contents, sheet, header_row, filename, state):
        state = dict(state or _default_state())
        trigger = ctx.triggered_id
        if trigger == ids.WIZ_MODE:
            if mode == state.get("mode"):
                return no_update
            state["mode"] = mode
        elif trigger == ids.WIZ_UPLOAD:
            if not contents:
                return no_update
            state["token"] = uploads.put_data_url(filename or "upload", contents)
            state["file_name"] = filename or "upload"
            state["sheet"] = None
            state["columns"] = []
            base = (filename or "new_form").rsplit(".", 1)[0]
            state["name"] = sanitize_identifier(base, fallback="new_form")
            state["display_name"] = humanize(state["name"])
        elif trigger == ids.WIZ_SHEET:
            if sheet == state.get("sheet") or sheet is None:
                return no_update
            state["sheet"] = sheet
            state["columns"] = []
            if sheet not in (None, "data"):
                state["name"] = sanitize_identifier(sheet, fallback=state["name"])
                state["display_name"] = humanize(state["name"])
        elif trigger == ids.WIZ_HEADER_ROW:
            if int(header_row or 1) == int(state.get("header_row") or 1):
                return no_update
            state["header_row"] = int(header_row or 1)
            state["columns"] = []
        return state

    @app.callback(
        Output(ids.WIZ_STORE, "data", allow_duplicate=True),
        Output(ids.WIZ_ERRORS, "children"),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.WIZ_NEXT, "n_clicks"),
        Input(ids.WIZ_BACK, "n_clicks"),
        Input(ids.WIZ_CANCEL, "n_clicks"),
        Input(ids.WIZ_CREATE, "n_clicks"),
        Input(ids.WIZ_ADD_COLUMN, "n_clicks"),
        State(ids.WIZ_STORE, "data"),
        State(ids.WIZ_COLUMNS_GRID, "virtualRowData"),
        State(ids.WIZ_COLUMNS_GRID, "rowData"),
        State(ids.WIZ_FUNCTION, "value"),
        State(ids.WIZ_NAME, "value"),
        State(ids.WIZ_DISPLAY, "value"),
        State(ids.WIZ_DESC, "value"),
        State(ids.WIZ_OWNER, "value"),
        State(ids.WIZ_LOAD_ROWS, "checked"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def navigate(
        n_next,
        n_back,
        n_cancel,
        n_create,
        n_add,
        state,
        virtual_rows,
        row_data,
        function,
        name,
        display,
        desc,
        owner,
        load_rows,
        nav_version,
        persona,
    ):
        trigger = ctx.triggered_id
        clicks = {
            ids.WIZ_NEXT: n_next,
            ids.WIZ_BACK: n_back,
            ids.WIZ_CANCEL: n_cancel,
            ids.WIZ_CREATE: n_create,
            ids.WIZ_ADD_COLUMN: n_add,
        }
        if not clicks.get(trigger):
            return (no_update,) * 5
        state = dict(state or _default_state())
        step = int(state.get("step", 0))
        grid_rows = virtual_rows or row_data
        if trigger == ids.WIZ_CANCEL:
            uploads.drop(state.get("token"))
            return _default_state(), None, "/", no_update, no_update
        if trigger == ids.WIZ_ADD_COLUMN:
            rows = list(grid_rows or state.get("columns") or [])
            n = len(rows) + 1
            rows.append(
                {
                    "name": f"column_{n}",
                    "type": "STRING",
                    "description": "",
                    "required": False,
                    "key": False,
                    "options": "",
                    "samples": "",
                    "source": "",
                }
            )
            state["columns"] = rows
            return state, None, no_update, no_update, no_update
        if trigger == ids.WIZ_BACK:
            if step == 1 and grid_rows is not None:
                state["columns"] = list(grid_rows)
            if step == 2:
                state.update(_details(function, name, display, desc, owner, load_rows))
            state["step"] = max(0, step - 1)
            return state, None, no_update, no_update, no_update
        if trigger == ids.WIZ_NEXT:
            if step == 0:
                if state["mode"] == "upload":
                    parsed = None
                    try:
                        parsed = _parsed(state)
                    except (ImportError_, ValueError) as exc:
                        return (
                            no_update,
                            error_alert(exc, "Cannot read the file"),
                            no_update,
                            no_update,
                            no_update,
                        )
                    if parsed is None:
                        return (
                            no_update,
                            info_alert("Upload a file to continue.", color="yellow"),
                            no_update,
                            no_update,
                            no_update,
                        )
                    if not state.get("columns"):
                        state["columns"] = _rows_from_parsed(parsed)
                    state["load_rows"] = True
                elif not state.get("columns"):
                    state["columns"] = [
                        {
                            "name": "code",
                            "type": "STRING",
                            "description": "Unique code",
                            "required": True,
                            "key": True,
                            "options": "",
                            "samples": "",
                            "source": "",
                        },
                        {
                            "name": "name",
                            "type": "STRING",
                            "description": "",
                            "required": True,
                            "key": False,
                            "options": "",
                            "samples": "",
                            "source": "",
                        },
                    ]
                    state["load_rows"] = False
                state["step"] = 1
                return state, None, no_update, no_update, no_update
            if step == 1:
                rows = list(grid_rows or [])
                _cols, errors = build_columns(rows)
                state["columns"] = rows
                if errors:
                    return (
                        state,
                        dmc.Alert(
                            dmc.List([dmc.ListItem(e) for e in errors], size="sm"),
                            color="red",
                            variant="light",
                        ),
                        no_update,
                        no_update,
                        no_update,
                    )
                state["step"] = 2
                return state, None, no_update, no_update, no_update
            if step == 2:
                state.update(_details(function, name, display, desc, owner, load_rows))
                if not state["name"]:
                    return (
                        state,
                        error_alert("A table name is required.", "Missing name"),
                        no_update,
                        no_update,
                        no_update,
                    )
                if not state["function"]:
                    return (
                        state,
                        error_alert("Choose a function.", "Missing function"),
                        no_update,
                        no_update,
                        no_update,
                    )
                state["step"] = 3
                return state, None, no_update, no_update, no_update
        if trigger == ids.WIZ_CREATE:
            form, columns, errors, mapping = assemble(state)
            if errors:
                return (
                    no_update,
                    error_alert(" ".join(errors), "Cannot create"),
                    no_update,
                    no_update,
                    no_update,
                )
            c = get_context(persona)
            frame = None
            try:
                if state["mode"] == "upload" and state.get("load_rows"):
                    parsed = _parsed(state)
                    if parsed is not None:
                        frame, _issues = coerce_frame(parsed.raw, columns, mapping)
                created = c.forms.create_form(form, frame if frame is not None and not frame.empty else None)
            except (BackendError, PermissionDenied, ValueError, ImportError_) as exc:
                return no_update, error_alert(exc, "Cannot create"), no_update, no_update, no_update
            uploads.drop(state.get("token"))
            invalidate_metadata()
            return (
                _default_state(),
                None,
                form_href(created.function, created.name),
                (nav_version or 0) + 1,
                notify(f"Form '{created.title}' created with {created.row_count or 0:,} rows"),
            )
        return (no_update,) * 5


def _details(function, name, display, desc, owner, load_rows) -> dict[str, Any]:
    clean = sanitize_identifier(name or "", fallback="") if (name or "").strip() else ""
    return {
        "function": function or "",
        "name": clean,
        "display_name": (display or "").strip() or (humanize(clean) if clean else ""),
        "description": desc or "",
        "owner": owner or "",
        "load_rows": bool(load_rows),
    }
