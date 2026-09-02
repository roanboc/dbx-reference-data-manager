"""Excel / CSV parsing and column type inference for the form creator and row import."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from rdm.models import (
    DEFAULT_DECIMAL_PRECISION,
    DEFAULT_DECIMAL_SCALE,
    ColumnDef,
    DataType,
    FormDef,
    ValidationIssue,
    sanitize_identifier,
)
from rdm.services.form_service import FALSE_WORDS, TRUE_WORDS, CoercionError, coerce_value, is_missing

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_IMPORT_ROWS = 100_000
OPTION_SUGGESTION_MAX_VALUES = 20
SAMPLE_VALUES = 5


class ImportError_(ValueError):
    """User-facing import problem."""


@dataclass
class ParsedSheet:
    sheet: str
    raw: pd.DataFrame  # as read, original headers
    columns: list[ColumnDef]  # inferred definitions, sanitised names
    source_names: dict[str, str]  # new column name -> original header
    samples: dict[str, list[str]] = field(default_factory=dict)
    suggested_options: dict[str, list[str]] = field(default_factory=dict)
    suggested_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def row_count(self) -> int:
        return len(self.raw)


def _is_excel(filename: str) -> bool:
    return filename.lower().endswith((".xlsx", ".xlsm", ".xls"))


def list_sheets(data: bytes, filename: str) -> list[str]:
    if not _is_excel(filename):
        return ["data"]
    with pd.ExcelFile(io.BytesIO(data)) as xl:
        return [str(s) for s in xl.sheet_names]


def read_table(data: bytes, filename: str, sheet: str | None = None, header_row: int = 0) -> pd.DataFrame:
    if len(data) > MAX_UPLOAD_BYTES:
        raise ImportError_(f"File is larger than {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    # dtype=object keeps codes such as "0042" and "1001" intact; keep_default_na=False stops
    # literal reference values like "NA" / "N/A" / "None" from silently becoming NULL.
    if _is_excel(filename):
        df = pd.read_excel(
            io.BytesIO(data),
            sheet_name=sheet or 0,
            header=header_row,
            dtype=object,
            keep_default_na=False,
            na_values=[""],
        )
    elif filename.lower().endswith((".csv", ".txt", ".tsv")):
        sep = "\t" if filename.lower().endswith(".tsv") else None
        df = pd.read_csv(
            io.BytesIO(data),
            header=header_row,
            sep=sep,
            engine="python",
            dtype=object,
            keep_default_na=False,
            na_values=[""],
        )
    else:
        raise ImportError_("Unsupported file type. Upload .xlsx, .xls or .csv.")
    df = df.dropna(how="all")
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed:") or df[c].notna().any()]]
    if len(df) > MAX_IMPORT_ROWS:
        raise ImportError_(f"The sheet has more than {MAX_IMPORT_ROWS:,} rows.")
    df.columns = [str(c).strip() for c in df.columns]
    return df.reset_index(drop=True)


def parse_file(data: bytes, filename: str, sheet: str | None = None, header_row: int = 0) -> ParsedSheet:
    raw = read_table(data, filename, sheet, header_row)
    if raw.shape[1] == 0:
        raise ImportError_("The sheet has no columns.")
    columns, source_names = infer_columns(raw)
    parsed = ParsedSheet(sheet=sheet or "data", raw=raw, columns=columns, source_names=source_names)
    for col in columns:
        series = raw[source_names[col.name]]
        non_null = series.dropna()
        parsed.samples[col.name] = [str(v) for v in non_null.head(SAMPLE_VALUES).tolist()]
        if col.data_type is DataType.STRING and len(non_null) >= 5:
            uniq = sorted({str(v).strip() for v in non_null if str(v).strip()})
            if 1 < len(uniq) <= OPTION_SUGGESTION_MAX_VALUES and len(uniq) <= len(non_null) / 2:
                parsed.suggested_options[col.name] = uniq
    for col in columns:
        series = raw[source_names[col.name]]
        if col.data_type in (DataType.STRING, DataType.INTEGER) and series.notna().all() and series.is_unique:
            parsed.suggested_keys.append(col.name)
            break
    if raw.empty:
        parsed.warnings.append("The sheet has headers but no data rows.")
    return parsed


def infer_columns(df: pd.DataFrame) -> tuple[list[ColumnDef], dict[str, str]]:
    columns: list[ColumnDef] = []
    source: dict[str, str] = {}
    used: set[str] = set()
    for i, header in enumerate(df.columns):
        name = sanitize_identifier(header, fallback=f"column_{i + 1}")
        base, k = name, 2
        while name in used:
            name = f"{base}_{k}"
            k += 1
        used.add(name)
        dtype, precision, scale = infer_type(df[header])
        columns.append(
            ColumnDef(name=name, data_type=dtype, precision=precision, scale=scale, nullable=True, position=i)
        )
        source[name] = str(header)
    return columns, source


def infer_type(series: pd.Series) -> tuple[DataType, int | None, int | None]:
    """Best-effort type inference for one column of an uploaded sheet."""
    s = series.dropna()
    if s.empty:
        return DataType.STRING, None, None
    if pd.api.types.is_bool_dtype(s):
        return DataType.BOOLEAN, None, None
    if pd.api.types.is_datetime64_any_dtype(s):
        ts = pd.to_datetime(s)
        if (ts.dt.normalize() == ts).all():
            return DataType.DATE, None, None
        return DataType.TIMESTAMP, None, None
    if pd.api.types.is_integer_dtype(s):
        return DataType.INTEGER, None, None
    if pd.api.types.is_float_dtype(s):
        return _numeric_type(s)
    # object column: look at the values
    values = [v for v in s.tolist() if not is_missing(v)]
    if not values:
        return DataType.STRING, None, None
    if all(isinstance(v, bool) for v in values):
        return DataType.BOOLEAN, None, None
    if all(isinstance(v, int | float) and not isinstance(v, bool) for v in values):
        return _numeric_type(pd.Series(values, dtype="float64"))
    strings = [str(v).strip() for v in values]
    lowered = {v.lower() for v in strings}
    if (
        lowered
        and lowered <= (TRUE_WORDS | FALSE_WORDS)
        and lowered & {"true", "false", "yes", "no", "y", "n"}
    ):
        return DataType.BOOLEAN, None, None
    numeric = pd.to_numeric(pd.Series(strings).str.replace(",", "", regex=False), errors="coerce")
    if numeric.notna().all():
        return _numeric_type(numeric.astype("float64"))
    if all(isinstance(v, datetime | date) for v in values):
        if all(
            isinstance(v, date)
            and not isinstance(v, datetime)
            or (isinstance(v, datetime) and v.time() == v.min.time())
            for v in values
        ):
            return DataType.DATE, None, None
        return DataType.TIMESTAMP, None, None
    if _looks_like_dates(strings):
        parsed = pd.to_datetime(pd.Series(strings), errors="coerce")
        if parsed.notna().all():
            if (parsed.dt.normalize() == parsed).all():
                return DataType.DATE, None, None
            return DataType.TIMESTAMP, None, None
    return DataType.STRING, None, None


def _looks_like_dates(strings: list[str]) -> bool:
    if not strings:
        return False
    sample = strings[:50]
    return all(any(ch in v for ch in "-/") and any(ch.isdigit() for ch in v) and len(v) >= 8 for v in sample)


def _numeric_type(s: pd.Series) -> tuple[DataType, int | None, int | None]:
    s = s.dropna().astype("float64")
    if s.empty:
        return DataType.DOUBLE, None, None
    if (s == s.round()).all() and (s.abs() < 2**62).all():
        return DataType.INTEGER, None, None
    decimals = 0
    for v in s.head(1000):
        text = f"{v:.10f}".rstrip("0").rstrip(".")
        if "." in text:
            decimals = max(decimals, len(text.split(".")[1]))
    if (
        decimals <= DEFAULT_DECIMAL_SCALE
        and (s.abs() < 10 ** (DEFAULT_DECIMAL_PRECISION - DEFAULT_DECIMAL_SCALE)).all()
    ):
        return DataType.DECIMAL, DEFAULT_DECIMAL_PRECISION, DEFAULT_DECIMAL_SCALE
    return DataType.DOUBLE, None, None


def coerce_frame(
    raw: pd.DataFrame, columns: list[ColumnDef], source_names: dict[str, str], max_issues: int = 50
) -> tuple[pd.DataFrame, list[ValidationIssue]]:
    """Convert the uploaded frame to the column definitions; invalid cells become empty."""
    out: dict[str, list[Any]] = {}
    issues: list[ValidationIssue] = []
    for col in columns:
        src = source_names.get(col.name, col.name)
        if src not in raw.columns:
            out[col.name] = [None] * len(raw)
            continue
        values = []
        for idx, v in enumerate(raw[src].tolist()):
            try:
                cv = coerce_value(col, v)
            except CoercionError as exc:
                cv = None
                if len(issues) < max_issues:
                    issues.append(ValidationIssue(f"Row {idx + 2}", col.name, str(exc)))
            if cv is None and col.required and len(issues) < max_issues:
                issues.append(ValidationIssue(f"Row {idx + 2}", col.name, "is required"))
            if cv is not None and col.options and str(cv) not in col.options and len(issues) < max_issues:
                issues.append(
                    ValidationIssue(f"Row {idx + 2}", col.name, f"must be one of: {', '.join(col.options)}")
                )
            values.append(cv)
        out[col.name] = values
    df = pd.DataFrame(out, columns=[c.name for c in columns])
    return df.astype(object).where(df.notna(), None), issues


def map_frame_to_form(raw: pd.DataFrame, form: FormDef) -> tuple[dict[str, str], list[str]]:
    """Match uploaded headers to an existing form's columns (for row import)."""
    mapping: dict[str, str] = {}
    unmatched: list[str] = []
    by_sanitised = {sanitize_identifier(h): h for h in raw.columns}
    for col in form.user_columns:
        if col.name in raw.columns:
            mapping[col.name] = col.name
        elif col.name in by_sanitised:
            mapping[col.name] = by_sanitised[col.name]
        else:
            unmatched.append(col.name)
    return mapping, unmatched
