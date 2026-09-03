"""Helpers shared by SQL backends: identifier quoting, literal escaping and type mapping.

Values are always bound as parameters. The helpers here exist for the places where SQL
cannot take parameters (identifiers, and DDL clauses such as COMMENT / TBLPROPERTIES).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from rdm.coercion import is_missing, to_naive_utc
from rdm.models import (
    CREATED_AT_COLUMN,
    DEFAULT_DECIMAL_PRECISION,
    DEFAULT_DECIMAL_SCALE,
    HISTORY_COLUMNS,
    ID_COLUMN,
    UPDATED_AT_COLUMN,
    UPDATED_BY_COLUMN,
    ColumnDef,
    DataType,
    FormDef,
    validate_identifier,
)


def quote_ident(name: str, quote: str = '"') -> str:
    """Validate and quote an identifier. ``quote`` is ``"`` for DuckDB and `` ` `` for Databricks."""
    validate_identifier(name)
    return f"{quote}{name}{quote}"


def qualified(parts: list[str], quote: str = '"') -> str:
    return ".".join(quote_ident(p, quote) for p in parts)


def search_pattern(text: str) -> str:
    """``LIKE`` pattern for a free-text search (wildcards in the text are escaped with ``\\``)."""
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def order_clause(form: FormDef, order_by: str | None, descending: bool, quote: str) -> str:
    """``ORDER BY`` for ``read_rows``: the requested column, else the business keys, else
    creation order; ``_id`` is the tie-breaker so paging is stable."""
    q = quote  # noqa: E741 - local alias
    direction = "DESC" if descending else "ASC"
    cols = [c.name for c in form.columns]
    if order_by and order_by in cols:
        order = f" ORDER BY {quote_ident(order_by, q)} {direction} NULLS LAST"
        return order + (f", {quote_ident(ID_COLUMN, q)}" if form.has_system_columns else "")
    if form.key_columns:
        keys = ", ".join(f"{quote_ident(k.name, q)} {direction} NULLS LAST" for k in form.key_columns)
        return f" ORDER BY {keys}, {quote_ident(ID_COLUMN, q)}"
    if form.has_system_columns:
        return f" ORDER BY {quote_ident(CREATED_AT_COLUMN, q)} {direction} NULLS LAST, {quote_ident(ID_COLUMN, q)}"
    return f" ORDER BY {quote_ident(cols[0], q)} {direction}" if cols else ""


def conflict_message(label: str, current: dict[str, Any] | None) -> str:
    """Why a row was skipped by a version-checked write (shown to the user as a conflict)."""
    if current is None:
        return f"{label}: the row was deleted by someone else."
    who = current.get(UPDATED_BY_COLUMN) or "someone else"
    when = to_db_scalar(current.get(UPDATED_AT_COLUMN))
    when_txt = f" at {when:%Y-%m-%d %H:%M:%S} UTC" if isinstance(when, datetime) else ""
    return f"{label}: modified by {who}{when_txt} after you loaded it."


def history_frame(rows: list[tuple], user_cols: list[str]) -> pd.DataFrame:
    """Decode audit rows ``(version, changed_at, changed_by, change_type, row_id, before_json,
    after_json)`` into the history frame both backends return (``get_history``)."""
    records = []
    for version, changed_at, changed_by, change_type, rid, before_json, after_json in rows:
        before = json.loads(before_json) if before_json else {}
        after = json.loads(after_json) if after_json else {}
        snapshot = after if change_type != "delete" else before
        changed = [c for c in user_cols if change_type == "update" and before.get(c) != after.get(c)]
        rec = {
            "version": version,
            "changed_at": to_db_scalar(changed_at),
            "changed_by": changed_by,
            "change_type": change_type,
            "changed_fields": ", ".join(changed),
            ID_COLUMN: rid,
        }
        for c in user_cols:
            rec[c] = snapshot.get(c)
        records.append(rec)
    return pd.DataFrame.from_records(
        records, columns=[*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, *user_cols]
    )


FILE_HISTORY_COLUMNS = ["version", "changed_at", "changed_by", "change_type", "size_bytes", "row_count"]


def file_history_frame(rows: list[tuple]) -> pd.DataFrame:
    """Decode audit rows ``(version, changed_at, changed_by, change_type, before_json, after_json)``
    of a file into the frame both backends return (``file_history``)."""
    records = []
    for version, changed_at, changed_by, change_type, before_json, after_json in rows:
        snapshot = json.loads(after_json) if after_json else (json.loads(before_json) if before_json else {})
        records.append(
            {
                "version": version,
                "changed_at": to_db_scalar(changed_at),
                "changed_by": changed_by,
                "change_type": change_type,
                "size_bytes": snapshot.get("size_bytes"),
                "row_count": snapshot.get("row_count"),
            }
        )
    return pd.DataFrame.from_records(records, columns=FILE_HISTORY_COLUMNS)


def escape_literal_duckdb(text: str) -> str:
    """DuckDB string literal: standard SQL, single quotes doubled, backslash is literal."""
    return "'" + str(text).replace("'", "''") + "'"


def escape_literal_databricks(text: str) -> str:
    """Databricks (Spark) SQL string literal: backslash escapes.

    Adjacent single-quoted literals are concatenated by the Spark parser, so the standard
    ``''`` doubling must not be used; ``\\'`` is the safe form.
    """
    s = str(text).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    return "'" + s + "'"


# --------------------------------------------------------------------------------------
# Type mapping
# --------------------------------------------------------------------------------------

_DECIMAL_RE = re.compile(r"^(?:DECIMAL|NUMERIC|DEC)\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)$", re.I)


def native_type_duckdb(col: ColumnDef) -> str:
    return _native_type(col, string="VARCHAR")


def native_type_databricks(col: ColumnDef) -> str:
    return _native_type(col, string="STRING")


def _native_type(col: ColumnDef, string: str) -> str:
    t = col.data_type
    if t is DataType.STRING:
        return string
    if t is DataType.INTEGER:
        return "BIGINT"
    if t is DataType.DECIMAL:
        p, s = col.decimal_params
        return f"DECIMAL({p},{s})"
    if t is DataType.DOUBLE:
        return "DOUBLE"
    if t is DataType.BOOLEAN:
        return "BOOLEAN"
    if t is DataType.DATE:
        return "DATE"
    if t is DataType.TIMESTAMP:
        return "TIMESTAMP"
    if t is DataType.OTHER and col.native_type:
        return col.native_type
    raise ValueError(f"Cannot map column {col.name!r} of type {t} to a native type.")


def parse_native_type(text: str | None) -> tuple[DataType, int | None, int | None]:
    """Map a native type string from either backend to ``(DataType, precision, scale)``."""
    if not text:
        return DataType.OTHER, None, None
    t = text.strip().upper()
    m = _DECIMAL_RE.match(t)
    if m:
        p = int(m.group(1))
        s = int(m.group(2)) if m.group(2) is not None else 0
        return DataType.DECIMAL, p, s
    if t in {"DECIMAL", "NUMERIC", "DEC"}:
        return DataType.DECIMAL, DEFAULT_DECIMAL_PRECISION, DEFAULT_DECIMAL_SCALE
    if t in {"VARCHAR", "STRING", "TEXT", "CHAR", "BPCHAR"} or t.startswith(("VARCHAR(", "CHAR(", "STRING(")):
        return DataType.STRING, None, None
    if t in {
        "BIGINT",
        "INT",
        "INTEGER",
        "SMALLINT",
        "TINYINT",
        "INT8",
        "INT4",
        "INT2",
        "INT1",
        "LONG",
        "SHORT",
        "BYTE",
        "HUGEINT",
        "UBIGINT",
        "UINTEGER",
        "USMALLINT",
        "UTINYINT",
    }:
        return DataType.INTEGER, None, None
    if t in {"DOUBLE", "FLOAT", "REAL", "FLOAT8", "FLOAT4", "DOUBLE PRECISION"}:
        return DataType.DOUBLE, None, None
    if t in {"BOOLEAN", "BOOL"}:
        return DataType.BOOLEAN, None, None
    if t == "DATE":
        return DataType.DATE, None, None
    if t.startswith("TIMESTAMP") or t == "DATETIME":
        return DataType.TIMESTAMP, None, None
    return DataType.OTHER, None, None


# --------------------------------------------------------------------------------------
# Value / frame normalisation shared by all backends
# --------------------------------------------------------------------------------------


def to_db_scalar(value: Any) -> Any:
    """Convert pandas/numpy scalars into plain Python values safe for parameter binding.

    ``None``/``pd.NA``/``NaT``/float NaN become ``None`` (a real NULL, never the string
    ``'nan'``); numpy scalars become Python scalars; timestamps become naive datetimes.
    """
    if isinstance(value, str | bytes | bool | int | Decimal):
        return value
    if is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return to_naive_utc(value)
    if hasattr(value, "item"):
        return value.item()
    return value


def normalise_frame(df: pd.DataFrame, form: FormDef) -> pd.DataFrame:
    """Give every column a dtype determined by its RDM type, whatever the driver returned.

    STRING -> object (None for missing); INTEGER -> Int64; DOUBLE -> float64;
    DECIMAL -> object of ``Decimal`` quantised to the column scale; BOOLEAN -> boolean;
    DATE -> object of ``datetime.date``; TIMESTAMP -> naive UTC ``datetime64[us]``.
    """
    out = df.copy()
    for c in form.columns:
        if c.name not in out.columns:
            continue
        col = out[c.name]
        t = c.data_type
        if t is DataType.STRING:
            out[c.name] = col.astype(object).where(col.notna(), None)
        elif t is DataType.INTEGER:
            out[c.name] = pd.to_numeric(col, errors="coerce").astype("Int64")
        elif t is DataType.DOUBLE:
            out[c.name] = pd.to_numeric(col, errors="coerce").astype("float64")
        elif t is DataType.DECIMAL:
            _p, scale = c.decimal_params
            quantum = Decimal(1).scaleb(-scale)

            def _dec(v: Any, q: Decimal = quantum) -> Decimal | None:
                v = to_db_scalar(v)
                if v is None:
                    return None
                try:
                    return Decimal(str(v)).quantize(q)
                except Exception:  # noqa: BLE001
                    return None

            out[c.name] = col.map(_dec).astype(object)
        elif t is DataType.BOOLEAN:
            out[c.name] = col.map(lambda v: None if to_db_scalar(v) is None else bool(v)).astype("boolean")
        elif t is DataType.DATE:
            ts = pd.to_datetime(col, errors="coerce")
            if getattr(ts.dt, "tz", None) is not None:
                ts = ts.dt.tz_localize(None)
            out[c.name] = ts.dt.date.astype(object).where(ts.notna(), None)
        elif t is DataType.TIMESTAMP:
            ts = pd.to_datetime(col, errors="coerce")
            if getattr(ts.dt, "tz", None) is not None:
                ts = ts.dt.tz_convert("UTC").dt.tz_localize(None)
            out[c.name] = ts.astype("datetime64[us]")
    return out
