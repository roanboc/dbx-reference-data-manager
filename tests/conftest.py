"""Shared fixtures: fresh in-memory DuckDB backends, the mock personas and a sample form.

Every test gets its own ``DuckDBBackend()`` so tests never share state. ``sample_form``
creates one form with every editable data type plus four rows whose ids are sequential
(``row-0001`` ...) so that ``read_rows`` returns them in insertion order.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from datetime import date, datetime

import pandas as pd
import pytest

from rdm import demo
from rdm.auth import PERSONAS
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.models import ColumnDef, DataType, DomainDef, FormDef, User

SAMPLE_DOMAIN = "test_domain"
SAMPLE_FORM = "products"
SAMPLE_OPTIONS = ["Hardware", "Software", "Service"]


def sample_columns() -> list[ColumnDef]:
    """User columns of the sample form: one of every editable type."""
    return [
        ColumnDef("code", DataType.STRING, "Product code", nullable=False, is_key=True),
        ColumnDef("category", DataType.STRING, "Product category", options=list(SAMPLE_OPTIONS)),
        ColumnDef("qty", DataType.INTEGER, "Quantity in stock"),
        ColumnDef("price", DataType.DECIMAL, "Unit price", precision=10, scale=2),
        ColumnDef("ratio", DataType.DOUBLE, "Conversion ratio"),
        ColumnDef("active", DataType.BOOLEAN, "Is active"),
        ColumnDef("start_date", DataType.DATE, "Available from"),
        ColumnDef("last_seen", DataType.TIMESTAMP, "Last stock check"),
    ]


def sample_rows() -> pd.DataFrame:
    """Four rows for the sample form; the last row has blanks in every optional column."""
    return pd.DataFrame(
        {
            "code": ["A001", "B002", "C003", "D004"],
            "category": pd.Series(["Hardware", "Software", "Service", None], dtype=object),
            "qty": pd.Series([10, 20, None, 40], dtype=object),
            "price": pd.Series([9.99, 120.5, 0.05, None], dtype=object),
            "ratio": pd.Series([0.5, 0.25, 1.0, None], dtype=object),
            "active": pd.Series([True, False, True, None], dtype=object),
            "start_date": pd.Series(
                [date(2024, 1, 1), date(2024, 2, 15), date(2023, 12, 31), None], dtype=object
            ),
            "last_seen": pd.Series(
                [
                    datetime(2024, 1, 1, 10, 30),
                    datetime(2024, 3, 5, 8, 0),
                    datetime(2024, 6, 30, 23, 59, 59),
                    None,
                ],
                dtype=object,
            ),
        }
    )


@pytest.fixture
def backend() -> Iterator[DuckDBBackend]:
    b = DuckDBBackend()
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def seeded_backend() -> Iterator[DuckDBBackend]:
    b = DuckDBBackend()
    demo.seed(b)
    try:
        yield b
    finally:
        b.close()


@pytest.fixture
def admin() -> User:
    return PERSONAS["admin"].user


@pytest.fixture
def editor() -> User:
    return PERSONAS["editor"].user


@pytest.fixture
def viewer() -> User:
    return PERSONAS["viewer"].user


@pytest.fixture
def sample_form(backend: DuckDBBackend, admin: User) -> FormDef:
    backend.create_domain(
        DomainDef(
            SAMPLE_DOMAIN, display_name="Test Domain", description="Fixture domain", owner="owner@example.org"
        ),
        admin,
    )
    counter = itertools.count(1)
    with pytest.MonkeyPatch.context() as mp:
        # Sequential ids keep read_rows() in insertion order (it orders by _created_at, _id).
        mp.setattr("rdm.backend.duckdb_backend.new_row_id", lambda: f"row-{next(counter):04d}")
        return backend.create_form(
            FormDef(
                SAMPLE_DOMAIN,
                SAMPLE_FORM,
                display_name="Products",
                description="Sample products",
                owner="owner@example.org",
                columns=sample_columns(),
            ),
            admin,
            sample_rows(),
        )
