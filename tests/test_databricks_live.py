"""Contract tests against a real Databricks SQL warehouse (docs/DEPLOYMENT.md §7).

Gated by ``RDM_TEST_DATABRICKS=1``: the suite creates and drops a throwaway function
(schema) and form (table) in the configured catalog, so it must only ever point at a dev
catalog. Authentication resolves through the Databricks SDK (``DATABRICKS_TOKEN`` or a CLI
profile via ``DATABRICKS_CONFIG_PROFILE``).

    RDM_TEST_DATABRICKS=1 RDM_CATALOG=<your dev catalog> \
    DATABRICKS_WAREHOUSE_ID=<id> DATABRICKS_CONFIG_PROFILE=<profile> \
    python -m pytest tests/test_databricks_live.py
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from conftest import sample_columns, sample_rows
from rdm.models import ChangeSet, FormDef, FunctionDef, RowDelete, RowInsert, RowUpdate, User

pytestmark = pytest.mark.skipif(
    os.environ.get("RDM_TEST_DATABRICKS") != "1",
    reason="live Databricks contract tests run only with RDM_TEST_DATABRICKS=1",
)

ACTOR = User(username="live-tests@rdm", display_name="Live contract tests", groups=("admins",))
FUNCTION = f"zz_live_{uuid.uuid4().hex[:8]}"
FORM = "products"


@pytest.fixture(scope="module")
def backend():
    from rdm.backend.databricks_backend import DatabricksBackend

    warehouse = os.environ.get("DATABRICKS_WAREHOUSE_ID")
    if not warehouse:
        pytest.skip("DATABRICKS_WAREHOUSE_ID is not set")
    catalog = os.environ.get("RDM_CATALOG")
    if not catalog:
        pytest.skip("RDM_CATALOG is not set; point it at a dev catalog")
    b = DatabricksBackend(
        catalog=catalog,
        http_path=f"/sql/1.0/warehouses/{warehouse}",
        host=os.environ.get("DATABRICKS_HOST"),
    )
    try:
        yield b
    finally:
        b.close()


@pytest.fixture(scope="module")
def live_form(backend):
    backend.create_function(
        FunctionDef(FUNCTION, display_name="Live test", description="Contract test scratch schema"),
        ACTOR,
    )
    try:
        form = backend.create_form(
            FormDef(FUNCTION, FORM, display_name="Products", columns=sample_columns()),
            ACTOR,
            rows=sample_rows(),
        )
        yield form
    finally:
        for f in backend.list_forms(FUNCTION):
            backend.drop_form(backend.get_form(FUNCTION, f.name), ACTOR)
        backend.drop_function(backend.get_function(FUNCTION), ACTOR)


def test_function_registry_roundtrip(backend, live_form):
    fn = backend.get_function(FUNCTION)
    assert fn.name == FUNCTION and fn.display_name == "Live test"
    assert FORM in [f.name for f in backend.list_forms(FUNCTION)]


def test_read_rows_types_and_order(backend, live_form):
    df = backend.read_rows(live_form)
    assert list(df["code"]) == ["A001", "B002", "C003", "D004"]
    assert df["qty"].dtype.name == "Int64" and df["qty"][2] is not None
    assert isinstance(df["price"][0], Decimal) and df["price"][0] == Decimal("9.99")
    assert df["start_date"][0] == date(2024, 1, 1)
    assert df["last_seen"][0] == datetime(2024, 1, 1, 10, 30)  # naive, session-zone UTC
    assert bool(df["active"][0]) is True and bool(df["active"][1]) is False
    assert backend.count_rows(live_form) == 4


def test_apply_changes_merge(backend, live_form):
    df = backend.read_rows(live_form)
    row_b = df[df["code"] == "B002"].iloc[0]
    row_c = df[df["code"] == "C003"].iloc[0]
    result = backend.apply_changes(
        live_form,
        ChangeSet(
            inserts=[RowInsert({"code": "E005", "qty": 5, "active": True}, label="new row 1")],
            updates=[
                RowUpdate(row_b["_id"], {"qty": 21, "price": 130.25}, expected_version=int(row_b["_version"]))
            ],
            deletes=[RowDelete(row_c["_id"], expected_version=int(row_c["_version"]))],
        ),
        ACTOR,
    )
    assert result.ok, result.errors or result.conflicts
    assert (result.inserted, result.updated, result.deleted) == (1, 1, 1)
    after = backend.read_rows(live_form)
    assert set(after["code"]) == {"A001", "B002", "D004", "E005"}
    updated = after[after["code"] == "B002"].iloc[0]
    assert updated["qty"] == 21 and updated["price"] == Decimal("130.25")
    assert int(updated["_version"]) == int(row_b["_version"]) + 1


def test_stale_version_is_a_conflict_not_an_overwrite(backend, live_form):
    df = backend.read_rows(live_form)
    row = df[df["code"] == "B002"].iloc[0]
    stale = int(row["_version"]) - 1
    result = backend.apply_changes(
        live_form,
        ChangeSet(updates=[RowUpdate(row["_id"], {"qty": 999}, expected_version=stale, label="B002")]),
        ACTOR,
    )
    assert not result.ok and result.conflicts and result.updated == 0
    assert backend.read_rows(live_form).pipe(lambda d: d[d["code"] == "B002"].iloc[0]["qty"]) == 21


def test_history_records_the_changes(backend, live_form):
    history = backend.get_history(live_form, limit=50)
    assert not history.empty
    assert {"insert", "update", "delete"} <= set(history["change_type"])
    assert (history["changed_by"] == ACTOR.username).any()
    assert history["changed_at"].notna().all()
    # timestamps arrive in UTC and are recent
    newest = history["changed_at"].iloc[0]
    if getattr(newest, "tzinfo", None) is None:
        newest = newest.replace(tzinfo=UTC)
    assert abs((datetime.now(UTC) - newest).total_seconds()) < 600
