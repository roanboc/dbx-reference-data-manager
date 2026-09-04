"""Upload checks and local previews for files (rdm.services.files)."""

from __future__ import annotations

import io

import pandas as pd
import pytest

from rdm.services.files import FileError, check_upload, human_size, preview_bytes, validate_table


def test_check_upload_sanitises_and_validates():
    assert check_upload("GL Transactions.CSV", b"a,b\n1,2\n", 1) == "gl_transactions.csv"
    with pytest.raises(FileError, match="Unsupported file type"):
        check_upload("notes.txt", b"x", 1)
    with pytest.raises(FileError, match="empty"):
        check_upload("x.csv", b"", 1)
    with pytest.raises(FileError, match="larger than 1 MB"):
        check_upload("x.csv", b"x" * (1024 * 1024 + 1), 1)


def test_preview_bytes_csv_and_parquet():
    csv = b"code,amount\nA,1.5\nB,2\nC,3\n"
    frame = preview_bytes(csv, "csv", rows=2)
    assert frame.columns.tolist() == ["code", "amount"] and len(frame) == 2
    buffer = io.BytesIO()
    pd.DataFrame({"x": range(50), "y": ["v"] * 50}).to_parquet(buffer, index=False)
    frame = preview_bytes(buffer.getvalue(), "parquet", rows=10)
    assert frame.columns.tolist() == ["x", "y"] and len(frame) == 10
    with pytest.raises(FileError, match="cannot be read as PARQUET"):
        preview_bytes(b"not parquet", "parquet")


def test_human_size():
    assert human_size(None) == "-"
    assert human_size(512) == "512 B"
    assert human_size(2048) == "2.0 KB"
    assert human_size(5 * 1024 * 1024) == "5.0 MB"
    assert human_size(3 * 1024**3) == "3.0 GB"


def test_validate_table_flags_inconsistent_files():
    assert validate_table(b"code,amount\nA,1\nB,2\n", "csv") == []
    [ragged] = validate_table(b"a,b\n1,2,3\n", "csv")
    assert "cannot be read as CSV" in ragged
    [dupes] = validate_table(b"a,a\n1,2\n", "csv")
    assert "Duplicate column headers: a" in dupes
    [unnamed] = validate_table(b"a,\n1,2\n", "csv")
    assert "have no header" in unnamed
    [empty] = validate_table(b"a,b\n", "csv")
    assert "no rows" in empty
    [bad] = validate_table(b"not parquet", "parquet")
    assert "cannot be read as PARQUET" in bad
    buffer = io.BytesIO()
    pd.DataFrame({"x": [1], "y": ["v"]}).to_parquet(buffer, index=False)
    assert validate_table(buffer.getvalue(), "parquet") == []
