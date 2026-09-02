"""Unit tests for rdm.backend.sql_utils: quoting, literal escaping and type mapping."""

from __future__ import annotations

import pytest

from rdm.backend.sql_utils import (
    escape_literal_databricks,
    escape_literal_duckdb,
    native_type_databricks,
    native_type_duckdb,
    parse_native_type,
    qualified,
    quote_ident,
)
from rdm.models import DEFAULT_DECIMAL_PRECISION, DEFAULT_DECIMAL_SCALE, ColumnDef, DataType

# --------------------------------------------------------------------------------------
# Identifiers
# --------------------------------------------------------------------------------------


def test_quote_ident_default_and_backtick():
    assert quote_ident("cost_centre") == '"cost_centre"'
    assert quote_ident("cost_centre", "`") == "`cost_centre`"
    assert quote_ident("_id") == '"_id"'


@pytest.mark.parametrize(
    "bad", ['a"b', "a`b", "a;drop table x", "Abc", "1abc", "", "a b", "x" * 64, "a-b", "a.b"]
)
def test_quote_ident_rejects_bad_identifiers(bad):
    with pytest.raises(ValueError, match="Invalid identifier"):
        quote_ident(bad)


def test_qualified_quotes_every_part():
    assert qualified(["dom", "frm"]) == '"dom"."frm"'
    assert qualified(["_rdm_meta", "grants"], "`") == "`_rdm_meta`.`grants`"
    with pytest.raises(ValueError):
        qualified(["dom", "bad name"])


# --------------------------------------------------------------------------------------
# Literals
# --------------------------------------------------------------------------------------


def test_escape_literal_duckdb_doubles_single_quotes():
    assert escape_literal_duckdb("plain") == "'plain'"
    assert escape_literal_duckdb("it's") == "'it''s'"
    assert escape_literal_duckdb("''") == "''''''"
    assert escape_literal_duckdb("") == "''"
    # Backslash and newlines are literal in DuckDB.
    assert escape_literal_duckdb("a\\b\nc") == "'a\\b\nc'"
    assert escape_literal_duckdb(42) == "'42'"


def test_escape_literal_databricks_uses_backslash_escapes():
    assert escape_literal_databricks("plain") == "'plain'"
    assert escape_literal_databricks("it's") == "'it\\'s'"
    assert escape_literal_databricks("a\\b") == "'a\\\\b'"
    assert escape_literal_databricks("line1\nline2\r") == "'line1\\nline2\\r'"
    assert escape_literal_databricks("") == "''"
    assert escape_literal_databricks(3.5) == "'3.5'"


@pytest.mark.parametrize("text", ["it's", "''", "'", "a'b'c", "\\'", "'\\"])
def test_escape_literal_databricks_never_doubles_quotes(text):
    inner = escape_literal_databricks(text)[1:-1]
    assert "''" not in inner
    # Every quote inside the literal body is preceded by an escaping backslash.
    for i, ch in enumerate(inner):
        if ch == "'":
            assert inner[i - 1] == "\\"


# --------------------------------------------------------------------------------------
# Native type mapping
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("col", "duckdb", "databricks"),
    [
        (ColumnDef("c", DataType.STRING), "VARCHAR", "STRING"),
        (ColumnDef("c", DataType.INTEGER), "BIGINT", "BIGINT"),
        (ColumnDef("c", DataType.DECIMAL, precision=10, scale=2), "DECIMAL(10,2)", "DECIMAL(10,2)"),
        (ColumnDef("c", DataType.DECIMAL), "DECIMAL(18,4)", "DECIMAL(18,4)"),
        (ColumnDef("c", DataType.DOUBLE), "DOUBLE", "DOUBLE"),
        (ColumnDef("c", DataType.BOOLEAN), "BOOLEAN", "BOOLEAN"),
        (ColumnDef("c", DataType.DATE), "DATE", "DATE"),
        (ColumnDef("c", DataType.TIMESTAMP), "TIMESTAMP", "TIMESTAMP"),
        (ColumnDef("c", DataType.OTHER, native_type="STRUCT<a INT>"), "STRUCT<a INT>", "STRUCT<a INT>"),
    ],
)
def test_native_type_mapping(col, duckdb, databricks):
    assert native_type_duckdb(col) == duckdb
    assert native_type_databricks(col) == databricks


def test_native_type_other_without_native_text_fails():
    with pytest.raises(ValueError, match="Cannot map column 'c'"):
        native_type_duckdb(ColumnDef("c", DataType.OTHER))
    with pytest.raises(ValueError):
        native_type_databricks(ColumnDef("c", DataType.OTHER))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("DECIMAL(10,2)", (DataType.DECIMAL, 10, 2)),
        ("decimal(10, 2)", (DataType.DECIMAL, 10, 2)),
        ("NUMERIC(5,3)", (DataType.DECIMAL, 5, 3)),
        ("DEC(7)", (DataType.DECIMAL, 7, 0)),
        ("DECIMAL", (DataType.DECIMAL, DEFAULT_DECIMAL_PRECISION, DEFAULT_DECIMAL_SCALE)),
        ("VARCHAR", (DataType.STRING, None, None)),
        ("VARCHAR(255)", (DataType.STRING, None, None)),
        ("STRING", (DataType.STRING, None, None)),
        ("TEXT", (DataType.STRING, None, None)),
        ("char(3)", (DataType.STRING, None, None)),
        ("BIGINT", (DataType.INTEGER, None, None)),
        ("INT", (DataType.INTEGER, None, None)),
        ("INTEGER", (DataType.INTEGER, None, None)),
        ("SMALLINT", (DataType.INTEGER, None, None)),
        ("TINYINT", (DataType.INTEGER, None, None)),
        ("  int  ", (DataType.INTEGER, None, None)),
        ("DOUBLE", (DataType.DOUBLE, None, None)),
        ("FLOAT", (DataType.DOUBLE, None, None)),
        ("REAL", (DataType.DOUBLE, None, None)),
        ("DOUBLE PRECISION", (DataType.DOUBLE, None, None)),
        ("BOOLEAN", (DataType.BOOLEAN, None, None)),
        ("bool", (DataType.BOOLEAN, None, None)),
        ("DATE", (DataType.DATE, None, None)),
        ("TIMESTAMP", (DataType.TIMESTAMP, None, None)),
        ("TIMESTAMP_NTZ", (DataType.TIMESTAMP, None, None)),
        ("TIMESTAMP WITH TIME ZONE", (DataType.TIMESTAMP, None, None)),
        ("timestamp(6)", (DataType.TIMESTAMP, None, None)),
        ("DATETIME", (DataType.TIMESTAMP, None, None)),
        ("STRUCT<a: INT, b: STRING>", (DataType.OTHER, None, None)),
        ("ARRAY<STRING>", (DataType.OTHER, None, None)),
        ("MAP<STRING, INT>", (DataType.OTHER, None, None)),
        ("BLOB", (DataType.OTHER, None, None)),
        ("", (DataType.OTHER, None, None)),
        (None, (DataType.OTHER, None, None)),
    ],
)
def test_parse_native_type(text, expected):
    assert parse_native_type(text) == expected


@pytest.mark.parametrize("data_type", DataType.editable_types())
def test_native_type_round_trips_through_parse(data_type):
    col = ColumnDef("c", data_type, precision=12, scale=3)
    for mapper in (native_type_duckdb, native_type_databricks):
        parsed, p, s = parse_native_type(mapper(col))
        assert parsed is data_type
        if data_type is DataType.DECIMAL:
            assert (p, s) == (12, 3)
