"""AG Grid configuration derived from form metadata, and row serialisation.

These are pure functions (no Dash callbacks) so they can be unit-tested.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from rdm.models import (
    AUDIT_COLUMNS,
    ID_COLUMN,
    VERSION_COLUMN,
    DataType,
    FormDef,
    humanize,
)

AUDIT_LABELS = {
    "_created_at": "Created",
    "_created_by": "Created by",
    "_updated_at": "Modified",
    "_updated_by": "Modified by",
}
INVALID_CLASS = "rdm-invalid"
NEW_ROW_CLASS = "rdm-new-row"
INVALID_PREFIX = "_bad_"  # per-column boolean flags on a row: simple expressions the grid can evaluate
NEW_FLAG = "_new"


def to_json_value(value: Any) -> Any:
    """JSON-safe representation of a cell value for the browser grid."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bool):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return value


def flag_invalid(row: dict[str, Any], columns: list[str] | None) -> None:
    """Set ``_bad_<col>`` flags on a grid row (cleared first) for cell highlighting."""
    for key in [k for k in row if k.startswith(INVALID_PREFIX)]:
        row.pop(key)
    for col in columns or []:
        row[f"{INVALID_PREFIX}{col}"] = True


def rows_to_records(df: pd.DataFrame, form: FormDef) -> list[dict[str, Any]]:
    """Serialise a ``read_rows`` frame for ``AgGrid.rowData`` (keeps ``_id`` and ``_version``)."""
    records = []
    for rec in df.to_dict("records"):
        out = {k: to_json_value(v) for k, v in rec.items()}
        if ID_COLUMN in out and out[ID_COLUMN] is not None:
            out[ID_COLUMN] = str(out[ID_COLUMN])
        records.append(out)
    return records


def column_defs(
    form: FormDef, editable: bool, show_audit: bool, invalid_expr: bool = True
) -> list[dict[str, Any]]:
    """AG Grid ``columnDefs`` for a form. Types drive editors; descriptions become tooltips."""
    defs: list[dict[str, Any]] = []
    first_user_column = True
    for c in form.columns:
        if c.name in (ID_COLUMN, VERSION_COLUMN):
            continue
        if c.name in AUDIT_COLUMNS:
            defs.append(
                {
                    "field": c.name,
                    "headerName": AUDIT_LABELS.get(c.name, humanize(c.name.lstrip("_"))),
                    "editable": False,
                    "hide": not show_audit,
                    "cellClass": "rdm-audit",
                    "minWidth": 150,
                    "headerTooltip": c.description or None,
                }
            )
            continue
        d: dict[str, Any] = {
            "field": c.name,
            "headerName": humanize(c.name),
            "headerTooltip": _tooltip(c),
            "editable": editable and c.data_type is not DataType.OTHER,
            "minWidth": 120,
            "cellClassRules": {INVALID_CLASS: f"params.data.{INVALID_PREFIX}{c.name}"},
        }
        if c.is_key:
            d["pinned"] = "left"
            d["headerClass"] = "rdm-key-header"
        if first_user_column:
            # Row selection drives "Delete selected", "Bulk update" and the item form; viewers
            # can select a single row (to open it), editors many.
            d["checkboxSelection"] = True
            d["headerCheckboxSelection"] = editable
            first_user_column = False
        t = c.data_type
        if t is DataType.STRING and c.options:
            d["cellEditor"] = "agSelectCellEditor"
            d["cellEditorParams"] = {"values": [*c.options]}
        elif t is DataType.STRING:
            d["cellEditor"] = "agTextCellEditor"
        elif t is DataType.INTEGER:
            d["cellEditor"] = "agNumberCellEditor"
            d["cellEditorParams"] = {"precision": 0, "step": 1}
            d["type"] = "numericColumn"
            d["filter"] = "agNumberColumnFilter"
        elif t is DataType.DECIMAL:
            _p, s = c.decimal_params
            d["cellEditor"] = "agNumberCellEditor"
            d["cellEditorParams"] = {"precision": s}
            d["type"] = "numericColumn"
            d["filter"] = "agNumberColumnFilter"
            d["valueFormatter"] = {
                "function": f"params.value == null ? '' : d3.format(',.{s}f')(params.value)"
            }
        elif t is DataType.DOUBLE:
            d["cellEditor"] = "agNumberCellEditor"
            d["type"] = "numericColumn"
            d["filter"] = "agNumberColumnFilter"
        elif t is DataType.BOOLEAN:
            d["cellRenderer"] = "agCheckboxCellRenderer"
            d["cellEditor"] = "agCheckboxCellEditor"
            d["cellRendererParams"] = {"disabled": not editable}
        elif t is DataType.DATE:
            d["cellEditor"] = "agDateStringCellEditor"
            d["filter"] = "agDateColumnFilter"
        elif t is DataType.TIMESTAMP:
            d["cellEditor"] = "agTextCellEditor"
            d["headerTooltip"] = (d["headerTooltip"] or "") + " · format YYYY-MM-DD HH:MM:SS"
        else:
            d["editable"] = False
            d["headerName"] = f"{humanize(c.name)} (read-only)"
        defs.append(d)
    return defs


def _tooltip(c) -> str:
    flags = [c.type_label] + (["required"] if c.required else []) + (["business key"] if c.is_key else [])
    return (c.description + " · " if c.description else "") + " · ".join(flags)


def default_col_def(editable: bool) -> dict[str, Any]:
    return {
        "sortable": True,
        "filter": True,
        "resizable": True,
        "floatingFilter": False,
        "editable": editable,
        "wrapHeaderText": True,
        "autoHeaderHeight": True,
        "suppressKeyboardEvent": {"function": "false"},
    }


def grid_options(editable: bool) -> dict[str, Any]:
    return {
        "rowSelection": "multiple" if editable else "single",
        "suppressRowClickSelection": True,
        "undoRedoCellEditing": True,
        "undoRedoCellEditingLimit": 50,
        "stopEditingWhenCellsLoseFocus": True,
        "enterNavigatesVertically": True,
        "enterNavigatesVerticallyAfterEdit": True,
        "animateRows": True,
        "tooltipShowDelay": 300,
        "rowHeight": 34,
        "headerHeight": 38,
        "enableCellTextSelection": not editable,
        "ensureDomOrder": True,
        "suppressMovableColumns": False,
    }


def row_class_rules() -> dict[str, str]:
    """``AgGrid.rowClassRules`` (dash-ag-grid evaluates the expressions with ``params``)."""
    return {NEW_ROW_CLASS: f"params.data.{NEW_FLAG}"}


def history_column_defs(form: FormDef, selectable: bool = False) -> list[dict[str, Any]]:
    defs = [
        {
            "field": "version",
            "headerName": "#",
            "maxWidth": 90,
            "type": "numericColumn",
            "checkboxSelection": selectable,
        },
        {"field": "changed_at", "headerName": "When", "minWidth": 160},
        {"field": "changed_by", "headerName": "By", "minWidth": 160},
        {"field": "change_type", "headerName": "Change", "maxWidth": 110},
        {"field": "changed_fields", "headerName": "Fields changed", "minWidth": 140},
    ]
    for c in form.user_columns:
        defs.append({"field": c.name, "headerName": humanize(c.name), "minWidth": 120})
    defs.append({"field": ID_COLUMN, "headerName": "Row id", "minWidth": 120, "hide": True})
    return defs
