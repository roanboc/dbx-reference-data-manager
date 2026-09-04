"""Helpers for files (CSV / Parquet datasets): upload checks and a local preview of the first rows."""

from __future__ import annotations

import csv
import io

import pandas as pd

from rdm.models import FILE_FORMATS, sanitize_file_name, split_file_name

PREVIEW_ROWS = 20


class FileError(ValueError):
    """User-facing problem with an uploaded file."""


def check_upload(filename: str, data: bytes, max_mb: int) -> str:
    """Validate an upload and return the sanitised ``<identifier>.<format>`` file name."""
    name = sanitize_file_name(filename or "")
    fmt = split_file_name(name)[1]
    if fmt not in FILE_FORMATS:
        raise FileError(
            f"Unsupported file type. Upload a {' or '.join(f.upper() for f in FILE_FORMATS)} file."
        )
    if not data:
        raise FileError("The file is empty.")
    if len(data) > max_mb * 1024 * 1024:
        raise FileError(
            f"The file is larger than {max_mb} MB. Land larger files in the function's volume with the "
            "Databricks CLI or a pipeline; they appear here automatically."
        )
    return name


def validate_table(data: bytes, fmt: str) -> list[str]:
    """FR-42: problems that would stop the file being used as a consistent table.

    The whole file must parse (ragged CSV rows fail), every column needs a header and
    headers must be unique. Returns an empty list when the file is sound.
    """
    try:
        if fmt == "parquet":
            import pyarrow.parquet as pq

            reader = pq.ParquetFile(io.BytesIO(data))
            names = [str(n) for n in reader.schema_arrow.names]
            rows = reader.metadata.num_rows
        else:
            names, rows, ragged = _scan_csv(data)
            if ragged:
                return [ragged]
    except Exception as exc:  # noqa: BLE001 - any parse problem is the finding
        return [f"The file cannot be read as {fmt.upper()}: {exc}"]
    problems = []
    if not names:
        problems.append("The file has no columns.")
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        problems.append("Duplicate column headers: " + ", ".join(duplicates) + ".")
    unnamed = [n for n in names if not n.strip() or n.lower().startswith("unnamed:")]
    if unnamed:
        problems.append(f"{len(unnamed)} column(s) have no header.")
    if not rows:
        problems.append("The file has headers but no rows.")
    return problems


def _scan_csv(data: bytes) -> tuple[list[str], int, str]:
    """Header, row count and the first field-count inconsistency (empty string when none)."""
    sample = data[:65536].decode("utf-8", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(data.decode("utf-8", errors="replace")), dialect)
    header = [str(c) for c in next(reader, [])]
    rows = 0
    for line_no, record in enumerate(reader, start=2):
        if not record:
            continue  # blank line
        if len(record) != len(header):
            return header, rows, (
                f"The file cannot be read as CSV: line {line_no} has {len(record)} field(s) "
                f"where the header has {len(header)}."
            )
        rows += 1
    return header, rows, ""


def preview_bytes(data: bytes, fmt: str, rows: int = PREVIEW_ROWS) -> pd.DataFrame:
    """The first rows of an uploaded file, parsed locally (before it is stored)."""
    try:
        if fmt == "parquet":
            import pyarrow.parquet as pq

            reader = pq.ParquetFile(io.BytesIO(data))
            batch = next(reader.iter_batches(batch_size=rows), None)
            return batch.to_pandas() if batch is not None else pd.DataFrame(columns=reader.schema.names)
        return pd.read_csv(io.BytesIO(data), nrows=rows)
    except Exception as exc:  # noqa: BLE001 - any parser error is a user-facing problem
        raise FileError(f"The file cannot be read as {fmt.upper()}: {exc}") from exc


def human_size(n: int | None) -> str:
    if n is None:
        return "-"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:,.0f} {unit}" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} GB"
