"""Tests for rdm.services.excel_import with workbooks built in memory."""

from __future__ import annotations

import io
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import pytest

from rdm.models import ColumnDef, DataType, FormDef
from rdm.services import excel_import
from rdm.services.excel_import import (
    MAX_UPLOAD_BYTES,
    ImportError_,
    coerce_frame,
    infer_columns,
    infer_type,
    list_sheets,
    map_frame_to_form,
    parse_file,
    read_table,
)

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def xlsx_bytes(*sheets: tuple[str, pd.DataFrame], index: bool = False) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, df in sheets:
            df.to_excel(writer, sheet_name=name, index=index)
    return buf.getvalue()


def csv_bytes(df: pd.DataFrame, sep: str = ",") -> bytes:
    return df.to_csv(index=False, sep=sep).encode()


def types_of(df: pd.DataFrame) -> dict[str, DataType]:
    columns, _ = infer_columns(df)
    return {c.name: c.data_type for c in columns}


# --------------------------------------------------------------------------------------
# infer_type
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 2, 3], (DataType.INTEGER, None, None)),
        ([1.0, 2.0, None], (DataType.INTEGER, None, None)),  # Excel ints with blanks arrive as float
        ([1.5, 2.25, None], (DataType.DECIMAL, 18, 4)),
        ([0.1234, 10.5], (DataType.DECIMAL, 18, 4)),
        ([1.12345, 2.0], (DataType.DOUBLE, None, None)),
        ([1e15 + 0.5], (DataType.DOUBLE, None, None)),
        ([True, False], (DataType.BOOLEAN, None, None)),
        ([True, None, False], (DataType.BOOLEAN, None, None)),
        (pd.to_datetime(["2024-01-01", "2024-02-01"]), (DataType.DATE, None, None)),
        (
            pd.to_datetime(["2024-01-01 10:30", "2024-02-01"], format="mixed"),
            (DataType.TIMESTAMP, None, None),
        ),
        ([datetime(2024, 1, 1), datetime(2024, 2, 1)], (DataType.DATE, None, None)),
        ([datetime(2024, 1, 1, 8, 30), None], (DataType.TIMESTAMP, None, None)),
        (["yes", "no", "Yes"], (DataType.BOOLEAN, None, None)),
        (["TRUE", "false"], (DataType.BOOLEAN, None, None)),
        (["Y", "n", " y "], (DataType.BOOLEAN, None, None)),
        (["1", "0"], (DataType.INTEGER, None, None)),  # digits alone are numbers, not booleans
        (["1", "2", "1,000"], (DataType.INTEGER, None, None)),
        (["1.50", "2.25"], (DataType.DECIMAL, 18, 4)),
        (["3.14159"], (DataType.DOUBLE, None, None)),
        (["2024-01-01", "2024-02-15"], (DataType.DATE, None, None)),
        (["01/02/2024", "03/04/2024"], (DataType.DATE, None, None)),
        (["2024-01-01 10:30:00", "2024-02-15 00:00:00"], (DataType.TIMESTAMP, None, None)),
        (["2024-13-45", "2024-01-01"], (DataType.STRING, None, None)),  # looks like dates but unparsable
        (["12/2024"], (DataType.STRING, None, None)),
        (["abc", "1"], (DataType.STRING, None, None)),
        (["yes", "maybe"], (DataType.STRING, None, None)),
        (["a", "b", "a"], (DataType.STRING, None, None)),
        ([None, None], (DataType.STRING, None, None)),
        (["", " "], (DataType.STRING, None, None)),
        ([], (DataType.STRING, None, None)),
        ([1, "x"], (DataType.STRING, None, None)),
    ],
)
def test_infer_type(values, expected):
    series = (
        values
        if isinstance(values, pd.Series | pd.DatetimeIndex)
        else pd.Series(values, dtype=object if values and any(isinstance(v, str) for v in values) else None)
    )
    if isinstance(series, pd.DatetimeIndex):
        series = pd.Series(series)
    assert infer_type(series) == expected


def test_infer_type_object_numbers_and_bools():
    assert infer_type(pd.Series([1, 2, None], dtype=object)) == (DataType.INTEGER, None, None)
    assert infer_type(pd.Series([1.5, 2, None], dtype=object)) == (DataType.DECIMAL, 18, 4)
    assert infer_type(pd.Series([True, None], dtype=object)) == (DataType.BOOLEAN, None, None)
    assert infer_type(pd.Series([True, 1], dtype=object)) == (DataType.INTEGER, None, None)


def test_infer_type_datetime_objects_in_object_column():
    series = pd.Series([pd.Timestamp("2024-01-01 10:00"), pd.Timestamp("2024-01-02 11:00")], dtype=object)
    assert infer_type(series)[0] is DataType.TIMESTAMP


# --------------------------------------------------------------------------------------
# infer_columns / parse_file
# --------------------------------------------------------------------------------------


def test_infer_columns_sanitises_and_deduplicates_headers():
    df = pd.DataFrame(
        {
            "Name": ["a"],
            "name": ["b"],
            "NAME": ["c"],
            "Cost Centre (GBP)": [1.5],
            "2024 Budget": [10],
            "select": ["x"],
            "!!!": ["y"],
            "cost_centre_gbp": [2],  # collides with the sanitised header above
        }
    )
    columns, source = infer_columns(df)
    assert [c.name for c in columns] == [
        "name",
        "name_2",
        "name_3",
        "cost_centre_gbp",
        "col_2024_budget",
        "select_",
        "column_7",
        "cost_centre_gbp_2",
    ]
    assert source == {
        "name": "Name",
        "name_2": "name",
        "name_3": "NAME",
        "cost_centre_gbp": "Cost Centre (GBP)",
        "col_2024_budget": "2024 Budget",
        "select_": "select",
        "column_7": "!!!",
        "cost_centre_gbp_2": "cost_centre_gbp",
    }
    assert [c.position for c in columns] == list(range(8))
    assert all(c.nullable for c in columns)
    assert columns[3].data_type is DataType.DECIMAL and (columns[3].precision, columns[3].scale) == (18, 4)
    assert columns[4].data_type is DataType.INTEGER


def _catalogue_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Product Code": [f"P{i:03d}" for i in range(1, 11)],
            "Category": [
                "Hardware",
                "Software",
                "Hardware",
                "Service",
                "Software",
                "Hardware",
                "Service",
                "Software",
                "Hardware",
                "Service",
            ],
            "Description": [f"unique text {i}" for i in range(10)],
            "Quantity": list(range(10, 20)),
            "Price": [1.25, 2.5, 3.75, 5.0, 6.25, 7.5, 8.75, 10.0, 11.25, 12.5],
            "Available From": [datetime(2024, 1, i) for i in range(1, 11)],
            "Last Check": [datetime(2024, 1, i, 9, 30) for i in range(1, 11)],
            "Active": ["yes", "no"] * 5,
        }
    )


def test_parse_file_xlsx_infers_columns_samples_options_and_key():
    parsed = parse_file(xlsx_bytes(("Catalogue", _catalogue_frame())), "products.xlsx", sheet="Catalogue")
    assert parsed.sheet == "Catalogue"
    assert parsed.row_count == 10
    types = {c.name: c.data_type for c in parsed.columns}
    assert types == {
        "product_code": DataType.STRING,
        "category": DataType.STRING,
        "description": DataType.STRING,
        "quantity": DataType.INTEGER,
        "price": DataType.DECIMAL,
        "available_from": DataType.DATE,
        "last_check": DataType.TIMESTAMP,
        "active": DataType.BOOLEAN,
    }
    assert parsed.source_names["product_code"] == "Product Code"
    assert parsed.samples["product_code"] == ["P001", "P002", "P003", "P004", "P005"]
    assert parsed.samples["quantity"] == ["10", "11", "12", "13", "14"]
    assert parsed.suggested_options == {"category": ["Hardware", "Service", "Software"]}
    assert parsed.suggested_keys == ["product_code"]
    assert parsed.warnings == []


def test_parse_file_default_sheet_is_the_first_one():
    data = xlsx_bytes(("First", pd.DataFrame({"a": [1]})), ("Second", pd.DataFrame({"b": ["x"]})))
    assert [c.name for c in parse_file(data, "f.xlsx").columns] == ["a"]
    assert [c.name for c in parse_file(data, "f.xlsx", sheet="Second").columns] == ["b"]
    assert parse_file(data, "f.xlsx").sheet == "data"


def test_parse_file_key_suggestion_skips_columns_with_nulls_or_duplicates():
    df = pd.DataFrame(
        {
            "code": ["A", "B", None, "D", "E"],  # has a null
            "category": ["x", "x", "y", "y", "y"],  # not unique
            "amount": [1, 2, 3, 4, 5],  # integer, unique, complete
            "other_id": ["u1", "u2", "u3", "u4", "u5"],  # also a candidate, but only the first is suggested
        }
    )
    parsed = parse_file(csv_bytes(df), "f.csv")
    assert parsed.suggested_keys == ["amount"]
    assert parsed.samples["code"] == ["A", "B", "D", "E"]


def test_parse_file_option_suggestion_rules():
    n = 10
    df = pd.DataFrame(
        {
            "few": ["a", "b"] * (n // 2),  # 2 distinct of 10 -> suggested
            "half": [f"v{i // 2}" for i in range(n)],  # 5 distinct of 10 -> suggested (<= half)
            "too_many": [f"v{i}" for i in range(n)],  # unique -> not suggested
            "single": ["only"] * n,  # 1 distinct -> not suggested
            "sparse": ["a", "b", "a", None, None, None, None, None, None, None],  # fewer than 5 values
            "numbers": [1, 2] * (n // 2),  # not text -> never suggested
        }
    )
    parsed = parse_file(csv_bytes(df), "f.csv")
    assert parsed.suggested_options == {"few": ["a", "b"], "half": ["v0", "v1", "v2", "v3", "v4"]}


def test_parse_file_drops_empty_rows_and_unnamed_empty_columns():
    df = pd.DataFrame(
        {
            "Code": ["A", None, "B", None],
            "Value": [1, None, 2, None],
            "": [None, None, None, None],  # header-less and empty -> "Unnamed: 2" -> dropped
        }
    )
    parsed = parse_file(xlsx_bytes(("S", df)), "f.xlsx")
    assert parsed.row_count == 2
    assert [c.name for c in parsed.columns] == ["code", "value"]
    assert parsed.raw.index.tolist() == [0, 1]
    assert parsed.raw["Code"].tolist() == ["A", "B"]


def test_parse_file_keeps_unnamed_columns_that_contain_data():
    df = pd.DataFrame({"Code": ["A"], "": ["kept"]})
    parsed = parse_file(xlsx_bytes(("S", df)), "f.xlsx")
    assert [c.name for c in parsed.columns] == ["code", "unnamed_1"]
    assert parsed.source_names["unnamed_1"] == "Unnamed: 1"


def test_parse_file_warns_when_there_are_no_data_rows():
    parsed = parse_file(xlsx_bytes(("S", pd.DataFrame({"Code": [], "Value": []}))), "f.xlsx")
    assert parsed.row_count == 0
    assert parsed.warnings == ["The sheet has headers but no data rows."]
    assert [c.data_type for c in parsed.columns] == [DataType.STRING, DataType.STRING]
    assert parsed.samples == {"code": [], "value": []}


def test_parse_file_without_columns_fails():
    with pytest.raises(ImportError_, match="no columns"):
        parse_file(xlsx_bytes(("S", pd.DataFrame())), "f.xlsx")


def test_parse_file_with_header_row_offset():
    buf = io.BytesIO()
    pd.DataFrame([["Title row", None], ["Code", "Qty"], ["A", 1], ["B", 2]]).to_excel(
        buf, index=False, header=False
    )
    parsed = parse_file(buf.getvalue(), "f.xlsx", header_row=1)
    assert [c.name for c in parsed.columns] == ["code", "qty"]
    assert parsed.row_count == 2
    assert {c.name: c.data_type for c in parsed.columns}["qty"] is DataType.INTEGER


# --------------------------------------------------------------------------------------
# list_sheets / read_table
# --------------------------------------------------------------------------------------


def test_list_sheets():
    data = xlsx_bytes(("Alpha", pd.DataFrame({"a": [1]})), ("Beta", pd.DataFrame({"b": [1]})))
    assert list_sheets(data, "book.xlsx") == ["Alpha", "Beta"]
    assert list_sheets(data, "BOOK.XLSM") == ["Alpha", "Beta"]
    assert list_sheets(b"a,b\n1,2\n", "table.csv") == ["data"]
    assert list_sheets(b"", "table.tsv") == ["data"]


def test_read_table_csv_and_tsv_and_txt():
    df = pd.DataFrame({" Code ": ["A", "B"], "Qty": [1, 2]})
    for name, sep in (("t.csv", ","), ("t.tsv", "\t"), ("t.txt", ","), ("T.CSV", ";")):
        out = read_table(csv_bytes(df, sep=sep), name)
        assert out.columns.tolist() == ["Code", "Qty"], name
        # values are read as text (dtype=object) so that codes such as "0042" survive; typing happens in coerce_frame
        assert out["Code"].tolist() == ["A", "B"] and out["Qty"].tolist() == ["1", "2"]


def test_read_table_excel_sheet_selection_and_header_row():
    data = xlsx_bytes(("One", pd.DataFrame({"a": [1]})), ("Two", pd.DataFrame({"b": [2]})))
    assert read_table(data, "f.xlsx").columns.tolist() == ["a"]
    assert read_table(data, "f.xlsx", sheet="Two").columns.tolist() == ["b"]
    with pytest.raises(ValueError):
        read_table(data, "f.xlsx", sheet="Missing")


def test_read_table_unsupported_extension():
    with pytest.raises(ImportError_, match="Unsupported file type"):
        read_table(b"{}", "data.json")
    with pytest.raises(ImportError_):
        read_table(b"", "noextension")


def test_read_table_size_limit():
    too_big = b"x" * (MAX_UPLOAD_BYTES + 1)
    with pytest.raises(ImportError_, match="larger than 25 MB"):
        read_table(too_big, "big.csv")
    with pytest.raises(ImportError_):
        parse_file(too_big, "big.xlsx")


def test_read_table_row_limit(monkeypatch):
    monkeypatch.setattr(excel_import, "MAX_IMPORT_ROWS", 3)
    ok = csv_bytes(pd.DataFrame({"a": [1, 2, 3]}))
    assert len(read_table(ok, "f.csv")) == 3
    too_many = csv_bytes(pd.DataFrame({"a": [1, 2, 3, 4]}))
    with pytest.raises(ImportError_, match="more than 3 rows"):
        read_table(too_many, "f.csv")
    # fully empty rows do not count towards the limit
    padded = csv_bytes(pd.DataFrame({"a": [1, None, 2, None, 3]}))
    assert len(read_table(padded, "f.csv")) == 3


# --------------------------------------------------------------------------------------
# coerce_frame / map_frame_to_form
# --------------------------------------------------------------------------------------


def _target_columns() -> list[ColumnDef]:
    return [
        ColumnDef("code", DataType.STRING, nullable=False),
        ColumnDef("category", DataType.STRING, options=["A", "B"]),
        ColumnDef("qty", DataType.INTEGER),
        ColumnDef("price", DataType.DECIMAL, precision=6, scale=2),
        ColumnDef("start_date", DataType.DATE),
        ColumnDef("flag", DataType.BOOLEAN),
        ColumnDef("missing_in_file", DataType.STRING),
    ]


def test_coerce_frame_reports_issues_with_excel_row_numbers_and_blanks_invalid_cells():
    raw = pd.DataFrame(
        {
            "Code": ["A", None, "C"],
            "Category": ["A", "Z", None],
            "Qty": ["1", "abc", 3.0],
            "Price": ["1.005", "12345.67", None],
            "Start": ["2024-01-01", "not a date", pd.NaT],
            "Flag": ["yes", "maybe", None],
        }
    )
    source = {
        "code": "Code",
        "category": "Category",
        "qty": "Qty",
        "price": "Price",
        "start_date": "Start",
        "flag": "Flag",
    }
    df, issues = coerce_frame(raw, _target_columns(), source)
    assert df.columns.tolist() == [
        "code",
        "category",
        "qty",
        "price",
        "start_date",
        "flag",
        "missing_in_file",
    ]
    assert df["code"].tolist() == ["A", None, "C"]
    assert df["category"].tolist() == ["A", "Z", None]  # invalid options stay (reported), blanks stay None
    assert df["qty"].tolist() == [1, None, 3]
    assert df["price"].tolist()[0] == Decimal("1.01") and df["price"].tolist()[1] is None
    assert df["start_date"].tolist() == [date(2024, 1, 1), None, None]
    assert df["flag"].tolist() == [True, None, None]
    assert df["missing_in_file"].tolist() == [None, None, None]
    assert all(v is None or not pd.isna(v) for v in df["qty"].tolist())  # None, never NaN
    assert [str(i) for i in issues] == [
        "Row 3, column 'code': is required",
        "Row 3, column 'category': must be one of: A, B",
        "Row 3, column 'qty': 'abc' is not a valid whole number",
        "Row 3, column 'price': too large for DECIMAL(6,2)",
        "Row 3, column 'start_date': 'not a date' is not a valid date",
        "Row 3, column 'flag': must be yes/no",
    ]


def test_coerce_frame_caps_the_number_of_issues():
    raw = pd.DataFrame({"qty": ["a", "b", "c", "d"]})
    df, issues = coerce_frame(raw, [ColumnDef("qty", DataType.INTEGER)], {"qty": "qty"}, max_issues=2)
    assert len(issues) == 2 and [i.row_label for i in issues] == ["Row 2", "Row 3"]
    assert df["qty"].tolist() == [None] * 4


def test_coerce_frame_defaults_source_name_to_column_name():
    raw = pd.DataFrame({"qty": [1.0, 2.0]})
    df, issues = coerce_frame(raw, [ColumnDef("qty", DataType.INTEGER)], {})
    assert df["qty"].tolist() == [1, 2] and issues == []
    assert str(df.dtypes["qty"]) == "object"


def test_map_frame_to_form_matches_exact_and_sanitised_headers():
    form = FormDef(
        "dom",
        "frm",
        columns=[
            ColumnDef("_id"),
            ColumnDef("cost_centre_code"),
            ColumnDef("name"),
            ColumnDef("amount"),
            ColumnDef("missing"),
        ],
    )
    raw = pd.DataFrame({"Cost Centre Code": [1], "name": ["x"], "AMOUNT (GBP)": [2], "Unrelated": [3]})
    mapping, unmatched = map_frame_to_form(raw, form)
    assert mapping == {"cost_centre_code": "Cost Centre Code", "name": "name"}
    assert unmatched == ["amount", "missing"]
    raw2 = pd.DataFrame({"Amount": [1]})
    assert map_frame_to_form(raw2, form) == ({"amount": "Amount"}, ["cost_centre_code", "name", "missing"])


def test_map_frame_to_form_prefers_exact_header_over_sanitised():
    form = FormDef("dom", "frm", columns=[ColumnDef("name")])
    raw = pd.DataFrame({"Name": [1], "name": [2]})
    assert map_frame_to_form(raw, form) == ({"name": "name"}, [])


def test_end_to_end_upload_round_trip(backend, admin):
    """parse_file -> coerce_frame -> create_form(rows) -> read_rows."""
    from rdm.models import FunctionDef

    parsed = parse_file(xlsx_bytes(("Catalogue", _catalogue_frame())), "products.xlsx", sheet="Catalogue")
    for c in parsed.columns:
        if c.name in parsed.suggested_options:
            c.options = parsed.suggested_options[c.name]
        if c.name in parsed.suggested_keys:
            c.is_key = True
            c.nullable = False
    rows, issues = coerce_frame(parsed.raw, parsed.columns, parsed.source_names)
    assert issues == []
    backend.create_function(FunctionDef("dom"), admin)
    form = backend.create_form(FormDef("dom", "catalogue", columns=parsed.columns), admin, rows)
    assert form.row_count == 10
    assert form.column("category").options == ["Hardware", "Service", "Software"]
    assert form.column("product_code").is_key and not form.column("product_code").nullable
    df = backend.read_rows(form)
    assert sorted(df["product_code"]) == [f"P{i:03d}" for i in range(1, 11)]
    assert df["active"].map(bool).sum() == 5
    assert df["available_from"].min() == date(2024, 1, 1)
