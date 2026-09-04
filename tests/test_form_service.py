"""Tests for rdm.services.form_service: coercion, change-set building, validation and guards."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from conftest import SAMPLE_FUNCTION, sample_columns
from rdm.backend.base import BackendError, PermissionDenied
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.models import (
    ID_COLUMN,
    UPDATED_AT_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    RowInsert,
    SaveResult,
    User,
    system_columns,
)
from rdm.services.form_service import (
    CoercionError,
    EditorState,
    FormService,
    build_changeset,
    build_import_changeset,
    coerce_value,
    describe_row,
    is_missing,
)

# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def col(data_type: DataType, **kw) -> ColumnDef:
    return ColumnDef("c", data_type, **kw)


STRING, INTEGER, DOUBLE, BOOLEAN, DATE, TIMESTAMP = (
    col(DataType.STRING),
    col(DataType.INTEGER),
    col(DataType.DOUBLE),
    col(DataType.BOOLEAN),
    col(DataType.DATE),
    col(DataType.TIMESTAMP),
)
DECIMAL_10_2 = col(DataType.DECIMAL, precision=10, scale=2)
T_LOADED = datetime(2024, 1, 1, 12, 0, 0)


def make_form(columns: list[ColumnDef] | None = None) -> FormDef:
    return FormDef(
        "dom", "frm", columns=system_columns() + (columns if columns is not None else sample_columns())
    )


def make_snapshot(
    form: FormDef, rows: list[dict], loaded_at: datetime | None = T_LOADED, version: int | None = 1
) -> pd.DataFrame:
    """A DataFrame shaped like ``backend.read_rows`` output for the given user values."""
    records = []
    for i, values in enumerate(rows, 1):
        rec = {
            ID_COLUMN: f"row-{i:04d}",
            VERSION_COLUMN: version,
            "_created_at": loaded_at,
            "_created_by": "seed",
            UPDATED_AT_COLUMN: loaded_at,
            "_updated_by": "seed",
        }
        rec.update({c.name: values.get(c.name) for c in form.user_columns})
        records.append(rec)
    df = pd.DataFrame(records, columns=[c.name for c in form.columns])
    for name in ("_created_at", UPDATED_AT_COLUMN):
        df[name] = pd.to_datetime(df[name])
    return df


SNAPSHOT_ROWS = [
    {
        "code": "A001",
        "category": "Hardware",
        "qty": 10,
        "price": 9.99,
        "ratio": 0.5,
        "active": True,
        "start_date": date(2024, 1, 1),
    },
    {
        "code": "B002",
        "category": "Software",
        "qty": 20,
        "price": 120.5,
        "ratio": 0.25,
        "active": False,
        "start_date": date(2024, 2, 15),
    },
    {
        "code": "C003",
        "category": "Service",
        "qty": None,
        "price": 0.05,
        "ratio": 1.0,
        "active": True,
        "start_date": None,
    },
]


# --------------------------------------------------------------------------------------
# is_missing / coerce_value
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", float("nan"), np.nan, pd.NA, pd.NaT, [], {}, ()])
def test_is_missing_true(value):
    assert is_missing(value)


@pytest.mark.parametrize("value", [0, False, "0", " x ", 0.0, [1], date(2024, 1, 1), Decimal(0)])
def test_is_missing_false(value):
    assert not is_missing(value)


@pytest.mark.parametrize("data_type", DataType)
@pytest.mark.parametrize("value", [None, "", "  ", float("nan"), pd.NA, pd.NaT])
def test_coerce_missing_is_none_for_every_type(data_type, value):
    assert coerce_value(col(data_type, precision=10, scale=2), value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  hello ", "hello"),
        ("a, b, c", "a, b, c"),
        (123.0, "123"),  # Excel reads "123" as 123.0
        (np.float64(456.0), "456"),
        (1.5, "1.5"),
        (7, "7"),
        (np.int64(8), "8"),
        (True, "True"),
        (date(2024, 1, 2), "2024-01-02"),
        (datetime(2024, 1, 2, 3, 4, 5), "2024-01-02T03:04:05"),
        (pd.Timestamp("2024-01-02 03:04:05"), "2024-01-02T03:04:05"),
        (Decimal("1.10"), "1.10"),
    ],
)
def test_coerce_string(value, expected):
    assert coerce_value(STRING, value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, 5),
        (np.int64(5), 5),
        (5.0, 5),
        (Decimal("7"), 7),
        ("42", 42),
        (" 1,000 ", 1000),
        ("1 000", 1000),
        ("12.0", 12),
        ("-3", -3),
        ("1e3", 1000),
        (True, 1),  # bools are accepted for whole numbers (see report: inconsistent with DECIMAL/DOUBLE)
        (False, 0),
    ],
)
def test_coerce_integer(value, expected):
    result = coerce_value(INTEGER, value)
    assert result == expected and type(result) is int


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (12.5, "whole number"),
        ("12.5", "whole number"),
        (Decimal("1.5"), "whole number"),
        ("abc", "not a valid whole number"),
        ("1/2", "not a valid"),
    ],
)
def test_coerce_integer_errors(value, message):
    with pytest.raises(CoercionError, match=message):
        coerce_value(INTEGER, value)


@pytest.mark.xfail(
    strict=True,
    reason="BUG rdm/services/form_service.py coerce_value: INTEGER strings go through float(), so BIGINT values "
    "above 2**53 are silently rounded (e.g. '9007199254740993' -> 9007199254740992).",
)
def test_coerce_integer_keeps_precision_above_2_pow_53():
    assert coerce_value(INTEGER, "9007199254740993") == 9007199254740993


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,234.567", Decimal("1234.57")),
        ("2.345", Decimal("2.35")),  # HALF_UP, not banker's rounding
        ("2.344", Decimal("2.34")),
        ("0.125", Decimal("0.13")),
        ("-0.125", Decimal("-0.13")),
        (1.1, Decimal("1.10")),
        (3, Decimal("3.00")),
        (np.float64(2.5), Decimal("2.50")),
        (Decimal("9999999.999"), Decimal("10000000.00")),
        (" 10 ", Decimal("10.00")),
    ],
)
def test_coerce_decimal(value, expected):
    result = coerce_value(DECIMAL_10_2, value)
    assert result == expected and isinstance(result, Decimal)
    assert result.as_tuple().exponent == -2


def test_coerce_decimal_scale_zero_and_default_scale():
    assert coerce_value(col(DataType.DECIMAL, precision=5, scale=0), "12.5") == Decimal("13")
    assert coerce_value(col(DataType.DECIMAL), "1.23456") == Decimal("1.2346")


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("123456789.00", "too large for DECIMAL"),
        (
            Decimal("99999999.999"),
            "too large for DECIMAL",
        ),  # rounds to 100000000.00: 11 digits > precision 10
        (Decimal("9999999.999"), None),  # rounds to 10000000.00: exactly 10 digits -> fine
        ("abc", "not a valid decimal"),
        ("nan", "finite"),
        ("inf", "finite"),
        (True, "must be a number"),
        (False, "must be a number"),
    ],
)
def test_coerce_decimal_errors(value, message):
    if message is None:
        assert coerce_value(DECIMAL_10_2, value) == Decimal("10000000.00")
        return
    with pytest.raises(CoercionError, match=message):
        coerce_value(DECIMAL_10_2, value)


def test_coerce_decimal_too_large_after_rounding():
    with pytest.raises(CoercionError, match="too large for DECIMAL\\(5,2\\)"):
        coerce_value(col(DataType.DECIMAL, precision=5, scale=2), "999.995")
    assert coerce_value(col(DataType.DECIMAL, precision=5, scale=2), "999.994") == Decimal("999.99")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1,234.5", 1234.5),
        ("1 234.5", 1234.5),
        (3, 3.0),
        (np.int64(3), 3.0),
        (Decimal("0.1"), 0.1),
        (2.5, 2.5),
        ("-1e-3", -0.001),
    ],
)
def test_coerce_double(value, expected):
    result = coerce_value(DOUBLE, value)
    assert result == expected and type(result) is float


@pytest.mark.parametrize(
    ("value", "message"),
    [("abc", "not a valid floating point"), (True, "must be a number"), (False, "must be a number")],
)
def test_coerce_double_errors(value, message):
    with pytest.raises(CoercionError, match=message):
        coerce_value(DOUBLE, value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (np.bool_(True), True),
        (1, True),
        (0, False),
        (1.0, True),
        (Decimal(0), False),
        ("yes", True),
        ("No", False),
        (" Y ", True),
        ("n", False),
        ("TRUE", True),
        ("false", False),
        ("1", True),
        ("0", False),
        ("t", True),
        ("f", False),
        ("on", True),
        ("off", False),
    ],
)
def test_coerce_boolean(value, expected):
    result = coerce_value(BOOLEAN, value)
    assert result is expected


@pytest.mark.parametrize("value", ["maybe", 2, -1, 0.5, "yess", "10"])
def test_coerce_boolean_errors(value):
    with pytest.raises(CoercionError, match="must be yes/no"):
        coerce_value(BOOLEAN, value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-01-01", date(2024, 1, 1)),
        (" 2024-01-31 ", date(2024, 1, 31)),
        ("2024-01-15T10:30:00", date(2024, 1, 15)),
        ("2024-01-15 23:59:59", date(2024, 1, 15)),
        (date(2024, 3, 1), date(2024, 3, 1)),
        (datetime(2024, 3, 1, 8, 0), date(2024, 3, 1)),
        (pd.Timestamp("2024-03-01 08:00"), date(2024, 3, 1)),
        (pd.Timestamp("2024-03-01 08:00", tz="UTC"), date(2024, 3, 1)),
        ("20240105", date(2024, 1, 5)),
    ],
)
def test_coerce_date(value, expected):
    result = coerce_value(DATE, value)
    assert result == expected and type(result) is date


@pytest.mark.parametrize("value", ["not a date", "2024-13-45", "31/31/2024", 12345678901234567890])
def test_coerce_date_errors(value):
    with pytest.raises(CoercionError, match="not a valid date"):
        coerce_value(DATE, value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2024-01-15T10:30:00", datetime(2024, 1, 15, 10, 30)),
        ("2024-01-15 10:30", datetime(2024, 1, 15, 10, 30)),
        ("2024-01-15", datetime(2024, 1, 15)),
        ("2024-01-15T10:30:00+02:00", datetime(2024, 1, 15, 8, 30)),
        ("2024-01-15T10:30:00Z", datetime(2024, 1, 15, 10, 30)),
        (datetime(2024, 1, 15, 10, 30), datetime(2024, 1, 15, 10, 30)),
        (datetime(2024, 1, 15, 10, 30, tzinfo=timezone(timedelta(hours=-5))), datetime(2024, 1, 15, 15, 30)),
        (datetime(2024, 1, 15, 10, 30, tzinfo=UTC), datetime(2024, 1, 15, 10, 30)),
        (pd.Timestamp("2024-01-15 10:30:00.123456"), datetime(2024, 1, 15, 10, 30, 0, 123456)),
        (pd.Timestamp("2024-01-15 10:30", tz="Europe/Paris"), datetime(2024, 1, 15, 9, 30)),
        (date(2024, 1, 15), datetime(2024, 1, 15, 0, 0)),
    ],
)
def test_coerce_timestamp(value, expected):
    result = coerce_value(TIMESTAMP, value)
    assert result == expected
    assert type(result) is datetime and result.tzinfo is None


@pytest.mark.parametrize("value", ["not a time", "2024-99-99T00:00:00"])
def test_coerce_timestamp_errors(value):
    with pytest.raises(CoercionError, match="not a valid date & time"):
        coerce_value(TIMESTAMP, value)


def test_coerce_other_is_read_only():
    with pytest.raises(CoercionError, match="read-only"):
        coerce_value(col(DataType.OTHER, native_type="STRUCT<a INT>"), "x")
    assert coerce_value(col(DataType.OTHER), None) is None


# --------------------------------------------------------------------------------------
# EditorState / describe_row
# --------------------------------------------------------------------------------------


def test_editor_state_from_session_normalises_keys():
    raw = {
        "edited_rows": {"3": {"qty": "5"}, 1: {"code": "x"}},
        "added_rows": [{"code": "n"}],
        "deleted_rows": ["2", 4],
    }
    state = EditorState.from_session(raw)
    assert state.edited_rows == {3: {"qty": "5"}, 1: {"code": "x"}}
    assert state.added_rows == [{"code": "n"}]
    assert state.deleted_rows == [2, 4]
    assert not state.is_empty
    # copies, not views
    raw["added_rows"][0]["code"] = "changed"
    assert state.added_rows[0]["code"] == "n"


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {},
        {"edited_rows": {}, "added_rows": [], "deleted_rows": []},
        {"edited_rows": None, "added_rows": None, "deleted_rows": None},
    ],
)
def test_editor_state_from_session_empty(raw):
    state = EditorState.from_session(raw)
    assert state.is_empty
    assert (state.edited_rows, state.added_rows, state.deleted_rows) == ({}, [], [])


def test_describe_row_uses_business_keys():
    form = make_form()
    assert describe_row(form, {"code": "A001", "category": "Hardware"}) == "Row code=A001"
    assert describe_row(form, {"code": None}) == "Row code="
    two_keys = make_form([ColumnDef("a", is_key=True), ColumnDef("b", is_key=True)])
    assert describe_row(two_keys, {"a": 1, "b": "x"}) == "Row a=1, b=x"


def test_describe_row_without_keys_falls_back_to_text_then_id_then_fallback():
    form = make_form([ColumnDef("n", DataType.INTEGER), ColumnDef("name"), ColumnDef("other")])
    assert describe_row(form, {"n": 1, "name": "Widget", "other": "x"}) == "Row 'Widget'"
    assert describe_row(form, {"n": 1, "name": "", "other": "Second"}) == "Row 'Second'"
    assert describe_row(form, {"n": 1, ID_COLUMN: "abcdef01-2345"}) == "row abcdef01"
    assert describe_row(form, {"n": 1, ID_COLUMN: "abcdef01"}, fallback="row 7") == "row 7 abcdef01"
    assert describe_row(form, {"n": 1}, fallback="row 7") == "row 7"


def test_describe_row_truncates_long_values():
    form = make_form()
    label = describe_row(form, {"code": "x" * 100})
    assert label == "Row code=" + "x" * 39 + "…"


# --------------------------------------------------------------------------------------
# build_changeset
# --------------------------------------------------------------------------------------


def test_build_changeset_empty_state():
    form = make_form()
    changes, issues = build_changeset(form, make_snapshot(form, SNAPSHOT_ROWS), EditorState())
    assert changes.is_empty and issues == []


def test_build_changeset_maps_positions_to_ids_and_tokens():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(
        edited_rows={1: {"qty": "25", "category": "Service", "active": "no"}},
        deleted_rows=[2],
    )
    changes, issues = build_changeset(form, snapshot, state)
    assert issues == []
    assert changes.summary() == "1 edited, 1 deleted"
    [upd] = changes.updates
    assert upd.row_id == "row-0002"
    assert upd.changes == {"qty": 25, "category": "Service", "active": False}
    assert upd.expected_version == 1 and type(upd.expected_version) is int
    assert upd.label == "Row code=B002"
    [dele] = changes.deletes
    assert dele.row_id == "row-0003" and dele.expected_version == 1 and dele.label == "Row code=C003"


def test_build_changeset_expected_token_is_none_when_snapshot_has_no_version():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS, loaded_at=None, version=None)
    changes, _ = build_changeset(form, snapshot, EditorState(edited_rows={0: {"qty": 1}}, deleted_rows=[1]))
    assert changes.updates[0].expected_version is None
    assert changes.deletes[0].expected_version is None


def test_build_changeset_ignores_system_and_unknown_columns():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(edited_rows={0: {ID_COLUMN: "zzz", UPDATED_AT_COLUMN: "2030-01-01", "ghost": 1}})
    changes, issues = build_changeset(form, snapshot, state)
    assert changes.is_empty and issues == []
    state = EditorState(edited_rows={0: {"ghost": 1, "qty": 3}})
    changes, issues = build_changeset(form, snapshot, state)
    assert changes.updates[0].changes == {"qty": 3} and issues == []


def test_build_changeset_ignores_edits_to_deleted_and_out_of_range_positions():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(edited_rows={0: {"qty": 1}, 99: {"qty": 2}, -1: {"qty": 3}}, deleted_rows=[0, 99, -5])
    changes, issues = build_changeset(form, snapshot, state)
    assert issues == []
    assert changes.updates == []
    assert [d.row_id for d in changes.deletes] == ["row-0001"]


def test_build_changeset_required_options_and_type_issues():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(
        edited_rows={0: {"code": "", "category": "Toys", "qty": "abc", "price": "1e9", "active": "maybe"}}
    )
    changes, issues = build_changeset(form, snapshot, state)
    messages = sorted(str(i) for i in issues)
    assert messages == [
        "Row code=A001, column 'active': must be yes/no",
        "Row code=A001, column 'category': must be one of: Hardware, Software, Service",
        "Row code=A001, column 'code': is required",
        "Row code=A001, column 'price': too large for DECIMAL(10,2)",
        "Row code=A001, column 'qty': 'abc' is not a valid whole number",
    ]
    # values that fail coercion are left out; invalid-but-coercible ones are kept for the UI to show
    assert changes.updates[0].changes == {"code": None, "category": "Toys"}


def test_build_changeset_type_error_only_yields_no_update():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    changes, issues = build_changeset(form, snapshot, EditorState(edited_rows={0: {"qty": "abc"}}))
    assert changes.is_empty and len(issues) == 1


def test_build_changeset_other_column_is_read_only():
    form = make_form(
        [ColumnDef("code", is_key=True), ColumnDef("blob", DataType.OTHER, native_type="STRUCT<a INT>")]
    )
    snapshot = make_snapshot(form, [{"code": "A", "blob": None}])
    state = EditorState(edited_rows={0: {"blob": "x"}}, added_rows=[{"code": "B", "blob": "y"}])
    changes, issues = build_changeset(form, snapshot, state)
    assert [str(i) for i in issues] == ["Row code=A, column 'blob': column is read-only"]
    assert changes.updates == []
    assert changes.inserts[0].values == {"code": "B"}  # OTHER columns are skipped on insert


def test_build_changeset_inserts_cover_every_user_column():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(
        added_rows=[
            {
                "code": " E005 ",
                "qty": "5",
                "category": "Hardware",
                "start_date": "2024-06-01",
                "last_seen": "2024-06-01T08:00:00+01:00",
            }
        ]
    )
    changes, issues = build_changeset(form, snapshot, state)
    assert issues == []
    [ins] = changes.inserts
    assert ins.label == "New row 1"
    assert ins.values == {
        "code": "E005",
        "category": "Hardware",
        "qty": 5,
        "price": None,
        "ratio": None,
        "active": None,
        "start_date": date(2024, 6, 1),
        "last_seen": datetime(2024, 6, 1, 7, 0),
    }


def test_build_changeset_empty_insert_is_flagged():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    changes, issues = build_changeset(form, snapshot, EditorState(added_rows=[{}, {"qty": None, "code": ""}]))
    assert [str(i) for i in issues] == [
        "New row 1, column 'code': is required",
        "New row 1: is empty",
        "New row 2, column 'code': is required",
        "New row 2: is empty",
    ]
    assert len(changes.inserts) == 2


def test_build_changeset_duplicate_keys_among_inserts():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(added_rows=[{"code": "X1"}, {"code": " x1 "}, {"code": "X2"}])
    _, issues = build_changeset(form, snapshot, state)
    assert [str(i) for i in issues] == ["New row 2, column 'code': duplicates New row 1"]


def test_build_changeset_insert_duplicates_existing_row():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    _, issues = build_changeset(form, snapshot, EditorState(added_rows=[{"code": "a001"}]))
    assert [str(i) for i in issues] == ["New row 1, column 'code': duplicates Row code=A001"]


def test_build_changeset_update_duplicates_earlier_row():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    _, issues = build_changeset(form, snapshot, EditorState(edited_rows={1: {"code": "A001"}}))
    assert [str(i) for i in issues] == ["Row code=A001, column 'code': duplicates existing Row code=A001"]


@pytest.mark.xfail(
    strict=True,
    reason="BUG rdm/services/form_service.py _check_unique_keys: duplicate detection is order dependent - an "
    "edited row that duplicates a *later* unchanged row is not reported (only 'key in seen and rid in updates').",
)
def test_build_changeset_update_duplicates_later_row():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    _, issues = build_changeset(form, snapshot, EditorState(edited_rows={0: {"code": "B002"}}))
    assert len(issues) == 1 and "duplicates" in str(issues[0])


def test_build_changeset_delete_frees_key_and_update_can_change_key():
    form = make_form()
    snapshot = make_snapshot(form, SNAPSHOT_ROWS)
    state = EditorState(deleted_rows=[0], added_rows=[{"code": "A001"}], edited_rows={1: {"code": "B999"}})
    changes, issues = build_changeset(form, snapshot, state)
    assert issues == []
    assert changes.summary() == "1 added, 1 edited, 1 deleted"


def test_build_changeset_composite_keys_and_missing_keys_are_skipped():
    form = make_form(
        [ColumnDef("a", is_key=True), ColumnDef("b", DataType.INTEGER, is_key=True), ColumnDef("txt")]
    )
    snapshot = make_snapshot(form, [{"a": "x", "b": 1}, {"a": "x", "b": 2}])
    state = EditorState(added_rows=[{"a": "X", "b": "1.0"}, {"a": "X", "b": 3}, {"txt": "no key"}])
    _, issues = build_changeset(form, snapshot, state)
    assert [str(i) for i in issues] == ["New row 1, column 'a, b': duplicates Row a=x, b=1"]


def test_build_changeset_without_key_columns_skips_uniqueness():
    form = make_form([ColumnDef("name"), ColumnDef("n", DataType.INTEGER)])
    snapshot = make_snapshot(form, [{"name": "dup", "n": 1}])
    _, issues = build_changeset(form, snapshot, EditorState(added_rows=[{"name": "dup"}, {"name": "dup"}]))
    assert issues == []


def test_build_changeset_on_empty_snapshot():
    form = make_form()
    snapshot = make_snapshot(form, [])
    changes, issues = build_changeset(
        form,
        snapshot,
        EditorState(added_rows=[{"code": "A"}, {"code": "a"}], edited_rows={0: {"qty": 1}}, deleted_rows=[0]),
    )
    assert changes.updates == [] and changes.deletes == []
    assert len(changes.inserts) == 2
    assert [str(i) for i in issues] == ["New row 2, column 'code': duplicates New row 1"]


# --------------------------------------------------------------------------------------
# FormService guards (seeded backend)
# --------------------------------------------------------------------------------------

CUSTOMER = "customer__survey_service_improvement"
FINANCE = "finance__cost_management"
HR = "hr__reference"


def service(backend: DuckDBBackend, user: User) -> FormService:
    return FormService(backend, user, backend.get_permissions(user))


def test_service_roles_per_persona(seeded_backend, admin, editor, viewer):
    assert service(seeded_backend, viewer).role(CUSTOMER) is Role.VIEWER
    assert service(seeded_backend, viewer).role(FINANCE) is Role.NONE
    assert service(seeded_backend, editor).role(CUSTOMER) is Role.EDITOR
    assert service(seeded_backend, editor).role(FINANCE) is Role.VIEWER
    assert service(seeded_backend, editor).role(HR) is Role.NONE
    assert service(seeded_backend, admin).role(HR) is Role.ADMIN
    assert service(seeded_backend, admin).permissions.can_create_function


def test_viewer_cannot_save(seeded_backend, viewer):
    svc = service(seeded_backend, viewer)
    form = svc.get_form(CUSTOMER, "service_areas")
    with pytest.raises(
        PermissionDenied,
        match="Editor access to function 'customer__survey_service_improvement' is required \\(you have: Viewer\\)",
    ):
        svc.save(form, ChangeSet(inserts=[RowInsert({"area_code": "X"})]))
    with pytest.raises(PermissionDenied):
        svc.save(form, ChangeSet())  # even an empty save needs editor rights
    with pytest.raises(PermissionDenied):
        svc.append_rows(form, pd.DataFrame({"area_code": ["X"]}))
    assert seeded_backend.count_rows(form) == 5


def test_viewer_can_read_visible_functions_only(seeded_backend, viewer):
    svc = service(seeded_backend, viewer)
    form = svc.get_form(CUSTOMER, "service_areas")
    assert len(svc.load_rows(form)) == 5
    assert len(svc.load_rows(form, search="log")) == 1
    assert len(svc.load_rows(form, search="", limit=2)) == 2
    assert len(svc.history(form)) == 5
    with pytest.raises(
        PermissionDenied,
        match="Viewer access to function 'finance__cost_management' is required \\(you have: No access\\)",
    ):
        svc.get_form(FINANCE, "cost_centres")
    finance_form = seeded_backend.get_form(FINANCE, "cost_centres")
    with pytest.raises(PermissionDenied):
        svc.load_rows(finance_form)
    with pytest.raises(PermissionDenied):
        svc.history(finance_form)


def test_editor_cannot_administer(seeded_backend, editor):
    svc = service(seeded_backend, editor)
    form = svc.get_form(CUSTOMER, "service_areas")
    new_form = FormDef(CUSTOMER, "new_form", columns=[ColumnDef("x")])
    with pytest.raises(PermissionDenied, match="Function admin access to function"):
        svc.create_form(new_form)
    with pytest.raises(PermissionDenied):
        svc.update_form_metadata(form)
    with pytest.raises(PermissionDenied):
        svc.add_column(form, ColumnDef("extra"))
    with pytest.raises(PermissionDenied):
        svc.drop_column(form, "lead_email")
    with pytest.raises(PermissionDenied):
        svc.drop_form(form)
    with pytest.raises(PermissionDenied):
        svc.update_function(FunctionDef(CUSTOMER, description="x"))
    with pytest.raises(PermissionDenied):
        svc.list_function_grants(CUSTOMER)
    with pytest.raises(PermissionDenied):
        svc.grant_function_role(CUSTOMER, "someone", Role.VIEWER)
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_function(FunctionDef("new_function"))
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_domain(DomainDef("new_domain"))
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.delete_domain("customer")
    assert [f.name for f in seeded_backend.list_forms(CUSTOMER)] == ["service_areas", "survey_questions"]
    assert "new_function" not in [d.name for d in seeded_backend.list_functions()]
    assert [d.name for d in svc.list_domains()] == ["customer", "finance", "people", "research"]
    assert seeded_backend.get_form(CUSTOMER, "service_areas").column("lead_email") is not None


def test_function_admin_without_catalog_rights_cannot_create_or_delete(seeded_backend, function_admin):
    svc = service(seeded_backend, function_admin)
    assert svc.role(FINANCE) is Role.ADMIN and not svc.permissions.is_global_admin
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_function(FunctionDef("another"))
    # they administer their function ...
    created = svc.create_form(
        FormDef(
            FINANCE,
            "local_form",
            description="Local list",
            owner="fin@example.org",
            columns=[ColumnDef("x")],
        )
    )
    assert created.owner == "fin@example.org"
    assert dict(svc.list_function_grants(FINANCE))["finance_admins"] is Role.ADMIN
    assert "finance_admins" in svc.list_groups("finance")
    function = seeded_backend.get_function(FINANCE)
    function.description = "Edited by the function admin"
    assert svc.update_function(function).description == "Edited by the function admin"
    # ... but cannot delete forms or functions, nor move the function to another domain
    with pytest.raises(PermissionDenied, match="Deleting a form requires global administrator"):
        svc.drop_form(created)
    with pytest.raises(PermissionDenied, match="Deleting a function requires global administrator"):
        svc.drop_function(function)
    function.domain = "people"
    with pytest.raises(PermissionDenied, match="Assigning a function to a domain"):
        svc.update_function(function)
    assert seeded_backend.get_function(FINANCE).domain == "finance"
    assert seeded_backend.get_form(FINANCE, "local_form").name == "local_form"


def test_global_admin_can_create_domain_function_and_form_and_delete_them(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    domain = svc.create_domain(DomainDef("library_services", display_name="Library Services"))
    assert domain.owner == admin.username and domain.function_count == 0
    function = svc.create_function(
        FunctionDef(
            "library",
            display_name="Library",
            description="Library lists",
            owner="lib@example.org",
            domain="library_services",
        )
    )
    assert function.owner == "lib@example.org" and function.domain == "library_services"
    assert seeded_backend.get_domain("library_services").function_count == 1
    # permissions were resolved before the function existed; refresh them
    svc = service(seeded_backend, admin)
    form = svc.create_form(
        FormDef(
            "library",
            "loans",
            description="Active loans",
            owner="lib@example.org",
            columns=[ColumnDef("loan_id", nullable=False, is_key=True)],
        )
    )
    assert form.row_count == 0
    updated = svc.update_function(
        FunctionDef(
            "library",
            display_name="Library Services",
            description="Library lists",
            owner="lib@example.org",
            domain="customer",
        )
    )
    assert updated.display_name == "Library Services" and updated.domain == "customer"
    svc.grant_function_role("library", "library_readers", Role.VIEWER)
    assert ("library_readers", Role.VIEWER) in svc.list_function_grants("library")
    with pytest.raises(BackendError, match="still has 1 form"):
        svc.drop_function(updated)
    svc.drop_form(form)
    assert seeded_backend.list_forms("library") == []
    svc.drop_function(updated)
    assert "library" not in [f.name for f in seeded_backend.list_functions()]
    assert svc.update_domain(DomainDef("library_services", display_name="Libraries")).title == "Libraries"
    svc.delete_domain("library_services")
    assert "library_services" not in [d.name for d in svc.list_domains()]
    with pytest.raises(BackendError, match="still has"):
        svc.delete_domain("customer")


def test_editor_happy_path_save_through_seeded_backend(seeded_backend, editor):
    svc = service(seeded_backend, editor)
    form = svc.get_form(CUSTOMER, "service_areas")
    snapshot = svc.load_rows(form)
    onboarding_pos = int(snapshot.index[snapshot["area_code"] == "ONB"][0])
    field_pos = int(snapshot.index[snapshot["area_code"] == "FLD"][0])
    raw_state = {
        "edited_rows": {str(onboarding_pos): {"lead_email": "onb.lead@example.org", "target_score": "79"}},
        "added_rows": [{"area_code": "SPT", "area_name": "Sport", "is_active": "yes", "target_score": "70"}],
        "deleted_rows": [field_pos],
    }
    changes, issues = build_changeset(form, snapshot, EditorState.from_session(raw_state))
    assert issues == []
    assert changes.updates[0].label == "Row area_code=ONB"
    result = svc.save(form, changes)
    assert isinstance(result, SaveResult) and result.ok
    assert (result.inserted, result.updated, result.deleted) == (1, 1, 1)
    after = svc.load_rows(form)
    assert set(after["area_code"]) == {"SUP", "LOG", "BIL", "ONB", "SPT"}
    onb = after[after["area_code"] == "ONB"].iloc[0]
    assert onb["lead_email"] == "onb.lead@example.org" and int(onb["target_score"]) == 79
    assert onb["_updated_by"] == editor.username
    spt = after[after["area_code"] == "SPT"].iloc[0]
    assert bool(spt["is_active"]) is True and spt["_created_by"] == editor.username
    history = svc.history(form)
    assert history["change_type"].tolist()[:3] == ["delete", "update", "insert"]
    assert history["changed_by"].tolist()[:3] == [editor.username] * 3
    assert history.iloc[0]["area_code"] == "FLD"
    assert history.iloc[1]["changed_fields"] == "lead_email, target_score"
    # an empty save is a no-op that touches nothing
    assert svc.save(form, ChangeSet()) == SaveResult()
    assert svc.append_rows(form, pd.DataFrame({"area_code": ["IMP"], "area_name": ["Imported"]})) == 1
    assert seeded_backend.count_rows(form) == 6


def test_editor_stale_edit_is_reported_not_applied(seeded_backend, editor, admin):
    svc = service(seeded_backend, editor)
    form = svc.get_form(CUSTOMER, "service_areas")
    snapshot = svc.load_rows(form)
    pos = int(snapshot.index[snapshot["area_code"] == "SUP"][0])
    changes, _ = build_changeset(form, snapshot, EditorState(edited_rows={pos: {"target_score": 90}}))
    # someone else changes the same row in between
    other = service(seeded_backend, admin)
    other_changes, _ = build_changeset(
        form, other.load_rows(form), EditorState(edited_rows={pos: {"target_score": 91}})
    )
    assert other.save(form, other_changes).updated == 1
    result = svc.save(form, changes)
    assert not result.ok and result.updated == 0
    assert result.conflicts[0].startswith(f"Row area_code=SUP: modified by {admin.username}")
    row = svc.load_rows(form)
    assert int(row[row["area_code"] == "SUP"].iloc[0]["target_score"]) == 91


def test_service_with_explicit_permissions_object(backend, admin):
    backend.create_function(FunctionDef("dom"), admin)
    svc = FormService(backend, User("anyone"), Permissions({"dom": Role.EDITOR}))
    with pytest.raises(PermissionDenied):
        svc.create_form(FormDef("dom", "frm", columns=[ColumnDef("x")]))
    form = backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("x")]), admin)
    assert svc.save(form, ChangeSet(inserts=[RowInsert({"x": "1"})])).inserted == 1
    assert backend.read_rows(form)["_created_by"].tolist() == ["anyone"]


# --------------------------------------------------------------------------------------
# Files: guards per role
# --------------------------------------------------------------------------------------

CSV = b"code,amount\nA,1\nB,2\n"


def test_file_guards_per_role(seeded_backend, admin, function_admin, editor, viewer):
    # viewer on customer: preview / download / history, nothing else
    v = service(seeded_backend, viewer)
    finance_file = seeded_backend.get_file(FINANCE, "gl_transactions.csv")
    with pytest.raises(PermissionDenied):
        v.get_file(FINANCE, "gl_transactions.csv")
    with pytest.raises(PermissionDenied):
        v.preview_file(finance_file)
    # editor has Viewer on finance: can read, cannot replace
    e = service(seeded_backend, editor)
    assert e.get_file(FINANCE, "gl_transactions.csv").row_count == 2000
    assert len(e.preview_file(finance_file, limit=5)) == 5
    assert e.read_file(finance_file)[:10] == b"posting_id"
    assert [c.name for c in e.file_columns(finance_file)][:2] == ["posting_id", "posted_on"]
    assert e.file_history(finance_file)["change_type"].tolist() == ["upload"]
    assert [f.name for f in e.list_files(FINANCE)] == ["fx_rates.parquet", "gl_transactions.csv"]
    with pytest.raises(PermissionDenied, match="Editor access to function 'finance__cost_management'"):
        e.replace_file(finance_file, CSV)
    with pytest.raises(PermissionDenied):
        e.add_file(FileDef(CUSTOMER, "new.csv"), CSV)  # editor on customer, not admin
    # function admin on finance: add, replace, metadata; not delete
    fa = service(seeded_backend, function_admin)
    added = fa.add_file(
        FileDef(
            FINANCE,
            "budget.csv",
            display_name="Budget",
            description="Budget lines",
            owner="fin@example.org",
        ),
        CSV,
    )
    assert added.row_count == 2 and added.owner == "fin@example.org"
    replaced = fa.replace_file(added, CSV + b"C,3\n")
    assert replaced.row_count == 3 and replaced.display_name == "Budget"
    replaced.description = "annual budget"
    assert fa.update_file_metadata(replaced).description == "annual budget"
    with pytest.raises(PermissionDenied, match="Deleting a file requires global administrator"):
        fa.drop_file(replaced)
    with pytest.raises(PermissionDenied):
        fa.add_file(FileDef(CUSTOMER, "x.csv"), CSV)  # only viewer there
    # global admin deletes
    g = service(seeded_backend, admin)
    g.drop_file(replaced)
    assert "budget.csv" not in [f.name for f in seeded_backend.list_files(FINANCE)]
    assert seeded_backend.file_history(replaced)["change_type"].tolist() == ["delete", "replace", "upload"]


# --------------------------------------------------------------------------------------
# Metadata quality rules (FR-40 / FR-41)
# --------------------------------------------------------------------------------------


def test_descriptions_and_owner_are_mandatory(seeded_backend, function_admin):
    svc = service(seeded_backend, function_admin)
    with pytest.raises(ValueError, match="needs a description"):
        svc.create_form(FormDef(FINANCE, "x1", owner="fin", columns=[ColumnDef("a")]))
    with pytest.raises(ValueError, match="an owner"):
        svc.create_form(FormDef(FINANCE, "x1", description="d", columns=[ColumnDef("a")]))
    with pytest.raises(ValueError, match="does not look like an e-mail"):
        svc.create_form(
            FormDef(
                FINANCE, "x1", description="d", owner="fin", owner_email="nope", columns=[ColumnDef("a")]
            )
        )
    created = svc.create_form(
        FormDef(
            FINANCE,
            "x1",
            description="d",
            owner="fin",
            owner_email="team@example.org",
            columns=[ColumnDef("a", description="A value")],
        )
    )
    assert created.owner_email == "team@example.org"
    with pytest.raises(ValueError, match="needs a description"):
        svc.add_column(created, ColumnDef("extra"))
    with pytest.raises(ValueError, match="needs a description"):
        svc.update_file_metadata(FileDef(FINANCE, "gl_transactions.csv", owner="fin"))
    with pytest.raises(ValueError, match="The function needs"):
        svc.update_function(FunctionDef(FINANCE))


def test_scd2_toggle_requires_function_admin(seeded_backend, editor, function_admin):
    form = seeded_backend.get_form(FINANCE, "cost_centres")
    with pytest.raises(PermissionDenied, match="Function admin access"):
        service(seeded_backend, editor).set_scd2(form, True)
    enabled = service(seeded_backend, function_admin).set_scd2(form, True)
    assert enabled.scd2_enabled


# --------------------------------------------------------------------------------------
# Import modes (FR-43)
# --------------------------------------------------------------------------------------


def test_build_import_changeset_merges_and_replaces(backend, admin, sample_form):
    current = backend.read_rows(sample_form)
    incoming = pd.DataFrame({"code": ["A001", "E005"], "qty": [99, 5]})
    changes, problems = build_import_changeset(sample_form, current, incoming, "merge")
    assert problems == []
    assert [u.changes for u in changes.updates] == [{"qty": 99}]  # only the changed cell
    assert [i.values["code"] for i in changes.inserts] == ["E005"]
    assert not changes.deletes
    replace, problems = build_import_changeset(sample_form, current, incoming, "replace")
    assert problems == [] and len(replace.deletes) == 3  # B002, C003, D004
    # applied through the service: audit and versions as for grid edits
    svc = FormService(backend, admin, Permissions({SAMPLE_FUNCTION: Role.ADMIN}))
    result = svc.save(sample_form, replace)
    assert result.inserted == 1 and result.updated == 1 and result.deleted == 3
    rows = backend.read_rows(sample_form)
    assert sorted(rows["code"].tolist()) == ["A001", "E005"]
    assert int(rows[rows["code"] == "A001"]["qty"].iloc[0]) == 99


def test_build_import_changeset_rejects_bad_files(backend, admin, sample_form):
    current = backend.read_rows(sample_form)
    _, problems = build_import_changeset(
        sample_form, current, pd.DataFrame({"code": ["X", "X"], "qty": [1, 2]}), "merge"
    )
    assert any("repeats the key" in p for p in problems)
    _, problems = build_import_changeset(
        sample_form, current, pd.DataFrame({"code": [None], "qty": [1]}), "merge"
    )
    assert any("business key" in p and "empty" in p for p in problems)
    _, problems = build_import_changeset(
        sample_form, current, pd.DataFrame({"code": ["A001"], "category": ["Bogus"]}), "merge"
    )
    assert any("must be one of" in p for p in problems)
    keyless = FormDef(
        SAMPLE_FUNCTION,
        "keyless",
        columns=system_columns() + [ColumnDef("x")],
    )
    _, problems = build_import_changeset(keyless, current, pd.DataFrame({"x": ["1"]}), "merge")
    assert any("business key columns" in p for p in problems)
    with pytest.raises(ValueError, match="Unknown import mode"):
        build_import_changeset(sample_form, current, pd.DataFrame(), "upsert")
