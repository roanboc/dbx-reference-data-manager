"""Value coercion (rdm.coercion): missing values and every portable type."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from rdm.coercion import CoercionError, coerce_value, is_missing, rule_violation
from rdm.models import ColumnDef, DataType


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
        (
            True,
            1,
        ),  # bools are accepted for whole numbers (unlike DECIMAL/DOUBLE, see test_coerce_decimal_errors)
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


def test_coerce_integer_keeps_precision_above_2_pow_53():
    # BIGINT values must not go through float(): 2**53 + 1 would round silently
    assert coerce_value(INTEGER, "9007199254740993") == 9007199254740993
    assert coerce_value(INTEGER, Decimal("9007199254740993")) == 9007199254740993
    assert coerce_value(INTEGER, "1e3") == 1000 and coerce_value(INTEGER, "12.0") == 12


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


def test_rule_violation_required_and_allowed_values():
    required = col(DataType.STRING, nullable=False)
    options = col(DataType.STRING, options=["A", "B"])
    assert rule_violation(required, None) == "is required"
    assert rule_violation(col(DataType.STRING), None) is None
    assert rule_violation(options, "C") == "must be one of: A, B"
    assert rule_violation(options, "A") is None and rule_violation(options, None) is None
