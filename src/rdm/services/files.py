"""Helpers for files (CSV / Parquet datasets): upload checks and a local preview of the first rows."""

from __future__ import annotations

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
    raise AssertionError("unreachable")
