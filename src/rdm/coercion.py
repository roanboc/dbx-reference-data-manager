"""Value coercion shared by the services (grid edits, Excel import) and the backends."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import pandas as pd

from rdm.models import ColumnDef, DataType

TRUE_WORDS = frozenset({"true", "yes", "y", "1", "t", "on"})
FALSE_WORDS = frozenset({"false", "no", "n", "0", "f", "off"})


class CoercionError(ValueError):
    """A value cannot be converted to the column's type."""


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    if isinstance(value, list | tuple | dict | set):
        return len(value) == 0
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def to_naive_utc(dt: datetime) -> datetime:
    """Timestamps are stored as naive UTC (docs/DESIGN.md §3.1)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


def rule_violation(col: ColumnDef, value: Any) -> str | None:
    """The column rule a coerced value breaks (required, allowed values), or ``None``.

    One rule set for the grid, the bulk update, the item form and the import
    (docs/FUNCTIONAL_DESIGN.md §7.4).
    """
    if value is None:
        return "is required" if col.required else None
    if col.options and str(value) not in col.options:
        return f"must be one of: {', '.join(col.options)}"
    return None


def coerce_value(col: ColumnDef, value: Any) -> Any:
    """Convert a raw grid/Excel value to the Python type the backends expect.

    Returns ``None`` for missing values; raises :class:`CoercionError` otherwise.
    """
    if is_missing(value):
        return None
    if hasattr(value, "item") and not isinstance(value, str | bytes | pd.Timestamp):
        value = value.item()  # numpy scalar -> python
    t = col.data_type
    try:
        if t is DataType.STRING:
            if isinstance(value, float) and value.is_integer():
                return str(int(value))  # Excel "123" arrives as 123.0
            if isinstance(value, datetime | date | pd.Timestamp):
                return value.isoformat()
            return str(value).strip()
        if t is DataType.INTEGER:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, int):
                return value
            if isinstance(value, float | Decimal):
                if value != int(value):
                    raise CoercionError("must be a whole number")
                return int(value)
            s = str(value).strip().replace(",", "").replace(" ", "")
            try:
                return int(s)  # exact: BIGINT values above 2**53 must not go through float()
            except ValueError:
                f = float(s)  # "12.0", "1e3"
                if not f.is_integer():
                    raise CoercionError("must be a whole number") from None
                return int(f)
        if t is DataType.DECIMAL:
            precision, scale = col.decimal_params
            if isinstance(value, bool):
                raise CoercionError("must be a number")
            d = Decimal(str(value).strip().replace(",", "").replace(" ", ""))
            if not d.is_finite():
                raise CoercionError("must be a finite number")
            d = d.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_HALF_UP)
            if len(d.as_tuple().digits) - scale > precision - scale:
                raise CoercionError(f"too large for DECIMAL({precision},{scale})")
            return d
        if t is DataType.DOUBLE:
            if isinstance(value, bool):
                raise CoercionError("must be a number")
            return (
                float(str(value).strip().replace(",", "").replace(" ", ""))
                if isinstance(value, str)
                else float(value)
            )
        if t is DataType.BOOLEAN:
            if isinstance(value, bool):
                return value
            if isinstance(value, int | float | Decimal):
                if value in (0, 1):
                    return bool(value)
                raise CoercionError("must be yes/no")
            s = str(value).strip().lower()
            if s in TRUE_WORDS:
                return True
            if s in FALSE_WORDS:
                return False
            raise CoercionError("must be yes/no")
        if t is DataType.DATE:
            if isinstance(value, pd.Timestamp):
                return value.date()
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            ts = pd.Timestamp(str(value).strip())
            return ts.date()
        if t is DataType.TIMESTAMP:
            if isinstance(value, pd.Timestamp):
                return to_naive_utc(value.to_pydatetime())
            if isinstance(value, datetime):
                return to_naive_utc(value)
            if isinstance(value, date):
                return datetime(value.year, value.month, value.day)
            return to_naive_utc(pd.Timestamp(str(value).strip()).to_pydatetime())
    except CoercionError:
        raise
    except (ValueError, TypeError, InvalidOperation, OverflowError) as exc:
        raise CoercionError(f"'{value}' is not a valid {col.data_type.label.lower()}") from exc
    raise CoercionError("column is read-only")
