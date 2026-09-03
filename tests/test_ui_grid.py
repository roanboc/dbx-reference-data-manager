"""Pure helpers behind the Dash grid and routing."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pandas as pd

from rdm.models import ID_COLUMN, VERSION_COLUMN, ColumnDef, DataType, FormDef, system_columns
from rdm.ui import grid
from rdm.ui.app import parse_path


def form() -> FormDef:
    return FormDef(
        "dom",
        "frm",
        columns=system_columns()
        + [
            ColumnDef("code", DataType.STRING, "Code", nullable=False, is_key=True),
            ColumnDef("category", DataType.STRING, options=["A", "B"]),
            ColumnDef("qty", DataType.INTEGER),
            ColumnDef("price", DataType.DECIMAL, precision=10, scale=2),
            ColumnDef("ratio", DataType.DOUBLE),
            ColumnDef("active", DataType.BOOLEAN),
            ColumnDef("start", DataType.DATE),
            ColumnDef("seen", DataType.TIMESTAMP),
            ColumnDef("blob", DataType.OTHER, native_type="STRUCT<a:int>"),
        ],
    )


def test_history_grid_options_selectable_only_for_editors():
    editor = grid.history_grid_options(selectable=True)
    assert editor["rowSelection"] == {
        "mode": "singleRow",
        "checkboxes": True,
        "headerCheckbox": False,
        "enableClickSelection": False,
    }
    assert "rowSelection" not in grid.history_grid_options(selectable=False)
    assert grid.history_grid_options(selectable=False, quick_filter="abc")["quickFilterText"] == "abc"
    assert grid.history_column_defs(form())[0]["field"] == "version"


def test_grid_options_selection_per_role():
    # editors tick many rows (and all at once); viewers tick one row to open it
    assert grid.grid_options(True)["rowSelection"] == {
        "mode": "multiRow",
        "checkboxes": True,
        "headerCheckbox": True,
        "enableClickSelection": False,
    }
    assert grid.grid_options(False)["rowSelection"]["mode"] == "singleRow"
    assert grid.grid_options(False)["rowSelection"]["headerCheckbox"] is False
    assert grid.grid_options(False)["enableCellTextSelection"]


def test_column_defs_types_editors_and_hidden_columns():
    defs = grid.column_defs(form(), editable=True, show_audit=False)
    fields = [d["field"] for d in defs]
    assert ID_COLUMN not in fields and VERSION_COLUMN not in fields
    assert fields[:4] == ["_created_at", "_created_by", "_updated_at", "_updated_by"]
    assert all(d["hide"] and not d["editable"] for d in defs[:4])
    by = {d["field"]: d for d in defs}
    assert by["code"]["pinned"] == "left" and by["code"]["headerClass"] == "rdm-key-header"
    assert by["category"]["cellEditor"] == "agSelectCellEditor" and by["category"]["cellEditorParams"][
        "values"
    ] == ["A", "B"]
    assert by["qty"]["cellEditor"] == "agNumberCellEditor" and by["qty"]["cellEditorParams"]["precision"] == 0
    assert (
        by["price"]["cellEditorParams"]["precision"] == 2
        and ",.2f" in by["price"]["valueFormatter"]["function"]
    )
    assert by["active"]["cellRenderer"] == "agCheckboxCellRenderer"
    assert by["start"]["cellEditor"] == "agDateStringCellEditor"
    assert by["blob"]["editable"] is False and "read-only" in by["blob"]["headerName"]
    assert by["code"]["cellClassRules"] == {grid.INVALID_CLASS: "params.data._bad_code"}
    assert "required" in by["code"]["headerTooltip"] and "business key" in by["code"]["headerTooltip"]


def test_column_defs_read_only_and_audit_visible():
    defs = grid.column_defs(form(), editable=False, show_audit=True)
    assert all(not d["editable"] for d in defs)
    assert not any(d.get("hide") for d in defs if d["field"].startswith("_"))


def test_rows_to_records_serialises_json_safely():
    df = pd.DataFrame(
        {
            ID_COLUMN: ["r1"],
            VERSION_COLUMN: pd.array([2], dtype="Int64"),
            "price": [Decimal("1.50")],
            "start": [date(2024, 1, 2)],
            "seen": [pd.Timestamp("2024-01-02 03:04:05")],
            "qty": pd.array([None], dtype="Int64"),
            "active": pd.array([True], dtype="boolean"),
        }
    )
    [rec] = grid.rows_to_records(df, form())
    assert rec == {
        ID_COLUMN: "r1",
        VERSION_COLUMN: 2,
        "price": 1.5,
        "start": "2024-01-02",
        "seen": "2024-01-02 03:04:05",
        "qty": None,
        "active": True,
    }


def test_to_json_value_handles_nat_and_datetime():
    assert grid.to_json_value(pd.NaT) is None
    assert grid.to_json_value(datetime(2024, 5, 6, 7, 8, 9)) == "2024-05-06 07:08:09"
    assert grid.to_json_value(float("nan")) is None


def test_flag_invalid_sets_and_clears_flags():
    row = {"code": "x", "_bad_qty": True}
    grid.flag_invalid(row, ["code"])
    assert row == {"code": "x", "_bad_code": True}
    grid.flag_invalid(row, None)
    assert row == {"code": "x"}


def test_parse_path_routes():
    assert parse_path("/") == ("home", None, None)
    assert parse_path(None) == ("home", None, None)
    assert parse_path("/f/finance__cost/cost_centres") == ("form", "finance__cost", "cost_centres")
    assert parse_path("/fn/hr__reference") == ("function", "hr__reference", None)
    assert parse_path("/file/finance__cost/gl.csv") == ("file", "finance__cost", "gl.csv")
    assert parse_path("/new-form") == ("new-form", None, None)
    assert parse_path("/new-form/finance__cost") == ("new-form", "finance__cost", None)
    assert parse_path("/new-file") == ("new-file", None, None)
    assert parse_path("/new-file/finance__cost") == ("new-file", "finance__cost", None)
    assert parse_path("/new-function") == ("new-function", None, None)
    assert parse_path("/domains") == ("domains", None, None)
    assert parse_path("/dm/finance") == ("domain", "finance", None)
    assert parse_path("/dm") == ("home", None, None)
    assert parse_path("/d/hr__reference") == ("home", None, None)  # old address, no longer routed
    assert parse_path("/f/only-function") == ("home", None, None)
    assert parse_path("/f/a%20b/c") == ("form", "a b", "c")


def test_parse_path_help():
    assert parse_path("/help") == ("help", None, None)
