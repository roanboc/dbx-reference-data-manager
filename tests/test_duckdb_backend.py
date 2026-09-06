"""Contract tests of :class:`DatabaseBackend` executed against the DuckDB implementation."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import duckdb
import pandas as pd
import pytest

from conftest import SAMPLE_FORM, SAMPLE_FUNCTION, sample_columns, sample_rows
from rdm.backend.base import BackendError, ConflictError, NotFoundError
from rdm.backend.duckdb_backend import CATALOG_LEVEL, META_SCHEMA, DuckDBBackend
from rdm.models import (
    CREATED_AT_COLUMN,
    CREATED_BY_COLUMN,
    HISTORY_COLUMNS,
    ID_COLUMN,
    PROP_COLUMN_CONFIG,
    PROP_DISPLAY_NAME,
    PROP_FORM,
    PROP_OWNER,
    SYSTEM_COLUMNS,
    UPDATED_AT_COLUMN,
    UPDATED_BY_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Role,
    RowDelete,
    RowInsert,
    RowUpdate,
    User,
)

T0 = datetime(2024, 1, 1, 12, 0, 0)
T1 = datetime(2024, 1, 2, 12, 0, 0)
T2 = datetime(2024, 1, 3, 12, 0, 0)


def _freeze(monkeypatch: pytest.MonkeyPatch, *times: datetime) -> None:
    """Make ``utcnow()`` return the given timestamps in order (the last one repeats)."""
    seq = list(times)

    def fake() -> datetime:
        return seq.pop(0) if len(seq) > 1 else seq[0]

    monkeypatch.setattr("rdm.backend.duckdb_backend.utcnow", fake)


def _row(df: pd.DataFrame, code: str) -> pd.Series:
    match = df[df["code"] == code]
    assert len(match) == 1, f"expected exactly one row with code {code}"
    return match.iloc[0]


def _version(row: pd.Series) -> int:
    """The optimistic-concurrency token of a row as loaded by ``read_rows``."""
    return int(row[VERSION_COLUMN])


def _meta_count(backend: DuckDBBackend, table: str, **where: str) -> int:
    clauses = " AND ".join(f"{k} = ?" for k in where)
    sql = f'SELECT count(*) FROM "{META_SCHEMA}"."{table}"' + (f" WHERE {clauses}" if where else "")
    return backend._conn.execute(sql, list(where.values())).fetchone()[0]


# ======================================================================================
# Domains
# ======================================================================================


def test_fresh_backend_has_no_domains_and_describes_itself(backend: DuckDBBackend):
    assert backend.list_functions() == []
    assert backend.name == "duckdb"
    assert backend.describe() == "DuckDB (:memory:)"


def test_create_function_returns_metadata_and_registers_it(backend: DuckDBBackend, admin: User):
    created = backend.create_function(
        FunctionDef(
            "finance",
            display_name="Finance",
            description="Money things",
            owner="cfo@example.org",
            doc_link="https://wiki.example.org/finance",
        ),
        admin,
    )
    assert created.name == "finance"
    assert created.display_name == "Finance"
    assert created.description == "Money things"
    assert created.owner == "cfo@example.org"
    assert created.doc_link == "https://wiki.example.org/finance"
    assert created.form_count == 0
    assert created.properties["created_by"] == admin.username
    assert created.properties[PROP_DISPLAY_NAME] == "Finance"
    # access comes from group grants only; creating a domain grants nothing to the creator
    assert backend.list_function_grants("finance") == []
    perms = backend.get_permissions(admin)
    assert perms.role_for("finance") is Role.NONE and not perms.is_global_admin
    assert _meta_count(backend, "functions", name="finance") == 1
    with pytest.raises(ValueError, match="http"):
        backend.create_function(FunctionDef("bad", doc_link="wiki/finance"), admin)
    updated = backend.update_function(FunctionDef("finance", display_name="Finance!", doc_link=""), admin)
    assert updated.display_name == "Finance!" and updated.doc_link == ""


def test_create_function_defaults_owner_to_actor(backend: DuckDBBackend, admin: User):
    d = backend.create_function(FunctionDef("finance"), admin)
    assert d.owner == admin.username
    assert d.display_name == "" and d.description == ""
    assert d.title == "Finance"


def test_get_function_not_found(backend: DuckDBBackend):
    with pytest.raises(NotFoundError, match="Function 'nope' does not exist"):
        backend.get_function("nope")


@pytest.mark.parametrize("hidden", ["main", "information_schema", META_SCHEMA, "pg_catalog"])
def test_hidden_schemas_are_not_functions(backend: DuckDBBackend, hidden: str):
    with pytest.raises(NotFoundError):
        backend.get_function(hidden)
    assert hidden not in [d.name for d in backend.list_functions()]


def test_create_function_duplicate_is_conflict(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    with pytest.raises(ConflictError, match="already exists"):
        backend.create_function(FunctionDef("finance"), admin)


@pytest.mark.parametrize("name", ["main", "information_schema", "pg_catalog", "temp"])
def test_create_function_reserved_names_are_conflicts(backend: DuckDBBackend, admin: User, name: str):
    with pytest.raises(ConflictError, match="reserved name"):
        backend.create_function(FunctionDef(name), admin)


@pytest.mark.parametrize("name", ["_rdm_meta", "_private", "Bad Name", "1st"])
def test_create_function_invalid_names_fail_validation(backend: DuckDBBackend, admin: User, name: str):
    with pytest.raises(ValueError):
        backend.create_function(FunctionDef(name), admin)
    assert backend.list_functions() == []


def test_list_functions_sorted_with_form_counts(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    backend.create_function(FunctionDef("alpha"), admin)
    backend.create_function(FunctionDef("zulu"), admin)
    domains = backend.list_functions()
    assert [d.name for d in domains] == ["alpha", SAMPLE_FUNCTION, "zulu"]
    assert {d.name: d.form_count for d in domains} == {"alpha": 0, SAMPLE_FUNCTION: 1, "zulu": 0}
    assert backend.get_function(SAMPLE_FUNCTION).form_count == 1


def test_update_function(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance", "Finance", "old", "old@example.org"), admin)
    updated = backend.update_function(
        FunctionDef("finance", "Finance & Co", "new text", "new@example.org"), admin
    )
    assert (updated.display_name, updated.description, updated.owner) == (
        "Finance & Co",
        "new text",
        "new@example.org",
    )
    assert updated.properties["created_by"] == admin.username  # untouched
    assert backend.get_function("finance").description == "new text"


def test_update_function_not_found(backend: DuckDBBackend, admin: User):
    with pytest.raises(NotFoundError):
        backend.update_function(FunctionDef("ghost"), admin)


# ======================================================================================
# Forms: create / get / list
# ======================================================================================


def test_create_form_adds_system_columns_in_front(backend: DuckDBBackend, sample_form: FormDef):
    names = [c.name for c in sample_form.columns]
    assert names[:6] == list(SYSTEM_COLUMNS)
    assert names[6:] == [c.name for c in sample_columns()]
    id_col = sample_form.column(ID_COLUMN)
    assert id_col.data_type is DataType.STRING and not id_col.nullable and id_col.is_system
    assert id_col.description == "Row identifier (generated)"
    assert sample_form.column(UPDATED_AT_COLUMN).data_type is DataType.TIMESTAMP
    assert sample_form.has_system_columns and sample_form.is_editable
    assert [c.position for c in sample_form.columns] == list(range(len(sample_form.columns)))


def test_create_form_persists_comments_properties_tags_and_column_config(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    assert form.description == "Sample products"
    assert form.display_name == "Products" and form.title == "Products"
    assert form.owner == "owner@example.org"
    assert form.column("code").description == "Product code"
    assert form.column("price").description == "Unit price"
    # properties
    assert form.properties[PROP_FORM] == "true"
    assert form.properties[PROP_DISPLAY_NAME] == "Products"
    assert form.properties[PROP_OWNER] == "owner@example.org"
    assert form.properties["created_by"] == admin.username
    assert datetime.fromisoformat(form.properties["created_at"])
    assert form.tags == {"rdm_form": "true", "rdm_display_name": "Products", "rdm_owner": "owner@example.org"}
    # column config round trip
    assert form.column("code").is_key and not form.column("code").nullable
    assert form.column("category").options == ["Hardware", "Software", "Service"]
    assert not form.column("category").is_key
    assert form.properties[PROP_COLUMN_CONFIG] == form.column_config_json()
    # native types
    types = {c.name: (c.data_type, c.native_type) for c in form.user_columns}
    assert types["code"] == (DataType.STRING, "VARCHAR")
    assert types["qty"] == (DataType.INTEGER, "BIGINT")
    assert types["price"] == (DataType.DECIMAL, "DECIMAL(10,2)")
    assert (form.column("price").precision, form.column("price").scale) == (10, 2)
    assert types["ratio"] == (DataType.DOUBLE, "DOUBLE")
    assert types["active"] == (DataType.BOOLEAN, "BOOLEAN")
    assert types["start_date"] == (DataType.DATE, "DATE")
    assert types["last_seen"] == (DataType.TIMESTAMP, "TIMESTAMP")


def test_create_form_defaults_owner_to_actor_and_reorders_system_columns(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    form = FormDef(
        "dom",
        "frm",
        columns=[ColumnDef("code"), ColumnDef("_id", DataType.STRING, "ignored", nullable=False)],
    )
    created = backend.create_form(form, admin)
    assert created.owner == admin.username
    assert created.tags["rdm_owner"] == admin.username
    assert [c.name for c in created.columns] == [*SYSTEM_COLUMNS, "code"]
    assert created.row_count == 0
    assert created.updated_at is None and created.updated_by == ""
    assert isinstance(created.created_at, datetime)


def test_create_form_missing_domain(backend: DuckDBBackend, admin: User):
    with pytest.raises(NotFoundError, match="Function 'ghost' does not exist"):
        backend.create_form(FormDef("ghost", "frm", columns=[ColumnDef("code")]), admin)


def test_create_form_duplicate_is_conflict(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    with pytest.raises(ConflictError, match="already exists"):
        backend.create_form(FormDef(SAMPLE_FUNCTION, SAMPLE_FORM, columns=[ColumnDef("code")]), admin)


def test_create_form_validates_definition(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    with pytest.raises(ValueError, match="at least one column"):
        backend.create_form(FormDef("dom", "frm"), admin)
    with pytest.raises(ValueError, match="reserved for system use"):
        backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("_secret")]), admin)
    with pytest.raises(ValueError, match="Duplicate column name"):
        backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("a"), ColumnDef("a")]), admin)
    assert backend.list_forms("dom") == []


def test_create_form_with_bad_rows_rolls_back_table_creation(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    form = FormDef("dom", "frm", columns=[ColumnDef("code", nullable=False), ColumnDef("name")])
    rows = pd.DataFrame({"code": ["A", None], "name": ["a", "b"]})
    with pytest.raises(BackendError, match="Import failed"):
        backend.create_form(form, admin, rows)
    assert backend.list_forms("dom") == []
    with pytest.raises(NotFoundError):
        backend.get_form("dom", "frm")
    assert _meta_count(backend, "object_properties", table_name="frm") == 0
    assert _meta_count(backend, "change_log", table_name="frm") == 0


def test_get_form_row_count_and_audit_fields(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    assert form.row_count == 4
    assert form.updated_by == admin.username
    rows = backend.read_rows(form)
    assert form.updated_at == rows[UPDATED_AT_COLUMN].max().to_pydatetime()
    assert form.created_at == datetime.fromisoformat(form.properties["created_at"])


def test_get_form_not_found(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    with pytest.raises(NotFoundError, match="Form 'dom.frm' does not exist"):
        backend.get_form("dom", "frm")
    with pytest.raises(NotFoundError):
        backend.get_form("ghost", "frm")


def test_list_forms_is_lightweight_and_sorted(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    backend.create_form(
        FormDef(SAMPLE_FUNCTION, "aardvark", display_name="Aardvarks", columns=[ColumnDef("x")]), admin
    )
    forms = backend.list_forms(SAMPLE_FUNCTION)
    assert [f.name for f in forms] == ["aardvark", SAMPLE_FORM]
    products = forms[1]
    assert products.columns == []  # no column metadata in the light listing
    assert products.display_name == "Products"
    assert products.description == "Sample products"
    assert products.owner == "owner@example.org"
    assert products.tags["rdm_form"] == "true"
    assert products.properties[PROP_FORM] == "true"
    assert products.row_count is None or isinstance(products.row_count, int)
    assert forms[0].title == "Aardvarks"


def test_list_forms_missing_function(backend: DuckDBBackend):
    with pytest.raises(NotFoundError):
        backend.list_forms("ghost")


def test_external_table_without_system_columns_is_read_only(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    backend._conn.execute('CREATE TABLE "dom"."legacy" (k INTEGER, payload STRUCT(a INTEGER), n VARCHAR)')
    backend._conn.execute("INSERT INTO \"dom\".\"legacy\" VALUES (2, {'a': 1}, 'two'), (1, {'a': 2}, 'one')")
    form = backend.get_form("dom", "legacy")
    assert not form.has_system_columns and not form.is_editable
    assert form.row_count == 2
    assert form.column("payload").data_type is DataType.OTHER
    assert form.column("payload").native_type.startswith("STRUCT")
    assert form.column("k").data_type is DataType.INTEGER
    rows = backend.read_rows(form)
    assert rows["k"].tolist() == [1, 2]  # ordered by the first column
    assert rows["n"].tolist() == ["one", "two"]
    assert backend.read_rows(form, search="TWO")["k"].tolist() == [2]
    with pytest.raises(BackendError, match="cannot be edited"):
        backend.apply_changes(form, ChangeSet(inserts=[RowInsert({"k": 3})]), admin)
    with pytest.raises(BackendError, match="cannot be edited"):
        backend.append_rows(form, pd.DataFrame({"k": [3]}), admin)
    assert backend.list_functions()[0].form_count == 1


# ======================================================================================
# read_rows
# ======================================================================================


def test_read_rows_returns_system_columns_and_normalised_types(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    df = backend.read_rows(sample_form)
    assert df.columns.tolist() == [c.name for c in sample_form.columns]
    assert df["code"].tolist() == ["A001", "B002", "C003", "D004"]
    assert str(df[CREATED_AT_COLUMN].dtype) == "datetime64[us]"
    assert str(df["start_date"].dtype) == "object"  # datetime.date objects
    assert str(df["last_seen"].dtype) == "datetime64[us]"
    assert str(df[VERSION_COLUMN].dtype) == "Int64" and df[VERSION_COLUMN].tolist() == [1, 1, 1, 1]
    assert df[CREATED_BY_COLUMN].tolist() == [admin.username] * 4
    assert df[ID_COLUMN].tolist() == ["row-0001", "row-0002", "row-0003", "row-0004"]
    last = _row(df, "D004")
    assert last["category"] is None
    assert int(last["qty"]) == 40 and pd.isna(last["price"]) and pd.isna(last["ratio"])
    assert pd.isna(last["active"]) and pd.isna(last["start_date"]) and pd.isna(last["last_seen"])
    first = _row(df, "A001")
    assert first["category"] == "Hardware"
    assert int(first["qty"]) == 10
    assert float(first["price"]) == pytest.approx(9.99)
    assert float(first["ratio"]) == 0.5
    assert bool(first["active"]) is True
    assert first["start_date"] == date(2024, 1, 1)
    assert first["last_seen"] == pd.Timestamp("2024-01-01 10:30:00")


def test_read_rows_orders_by_created_at_then_id(backend: DuckDBBackend, admin: User, monkeypatch):
    backend.create_function(FunctionDef("dom"), admin)
    form = backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("code")]), admin)
    _freeze(monkeypatch, T2, T0, T1)
    for code in ("zulu", "alpha", "mike"):  # created at T2, T0, T1
        backend.apply_changes(form, ChangeSet(inserts=[RowInsert({"code": code})]), admin)
    df = backend.read_rows(form)
    assert df["code"].tolist() == ["alpha", "mike", "zulu"]
    assert df[CREATED_AT_COLUMN].tolist() == [pd.Timestamp(T0), pd.Timestamp(T1), pd.Timestamp(T2)]


def test_read_rows_limit(backend: DuckDBBackend, sample_form: FormDef):
    df = backend.read_rows(sample_form, limit=2)
    assert df["code"].tolist() == ["A001", "B002"]
    assert len(backend.read_rows(sample_form, limit=0)) == 0


def test_read_rows_search_is_case_insensitive_and_spans_columns(backend: DuckDBBackend, sample_form: FormDef):
    assert backend.read_rows(sample_form, search="hardware")["code"].tolist() == ["A001"]
    assert backend.read_rows(sample_form, search="SOFT")["code"].tolist() == ["B002"]
    assert backend.read_rows(sample_form, search="b00")["code"].tolist() == ["B002"]
    # numeric and date columns are searched through their text form
    assert backend.read_rows(sample_form, search="40")["code"].tolist() == ["D004"]
    assert backend.read_rows(sample_form, search="2024-02-15")["code"].tolist() == ["B002"]
    # empty / whitespace-only search means no filter; unmatched search yields an empty frame
    assert len(backend.read_rows(sample_form, search="")) == 4
    assert len(backend.read_rows(sample_form, search=None)) == 4
    missing = backend.read_rows(sample_form, search="does-not-exist")
    assert missing.empty and missing.columns.tolist() == [c.name for c in sample_form.columns]


def test_read_rows_search_does_not_match_system_columns(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    assert backend.read_rows(sample_form, search="row-0001").empty
    assert backend.read_rows(sample_form, search=admin.username).empty


def test_read_rows_search_escapes_like_wildcards(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    backend.apply_changes(
        sample_form,
        ChangeSet(
            inserts=[
                RowInsert({"code": "100%"}),
                RowInsert({"code": "1000"}),
                RowInsert({"code": "a_b"}),
                RowInsert({"code": "axb"}),
                RowInsert({"code": "back\\slash"}),
            ]
        ),
        admin,
    )
    assert backend.read_rows(sample_form, search="100%")["code"].tolist() == ["100%"]
    assert backend.read_rows(sample_form, search="a_b")["code"].tolist() == ["a_b"]
    assert backend.read_rows(sample_form, search="back\\slash")["code"].tolist() == ["back\\slash"]
    assert backend.read_rows(sample_form, search="%")["code"].tolist() == ["100%"]


def test_count_rows(backend: DuckDBBackend, sample_form: FormDef):
    assert backend.count_rows(sample_form) == 4


# ======================================================================================
# apply_changes
# ======================================================================================


def test_apply_changes_insert_stamps_audit_columns(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    _freeze(monkeypatch, T1)
    values = {
        "code": "E005",
        "category": "Service",
        "qty": 7,
        "price": Decimal("12.34"),
        "ratio": 0.75,
        "active": False,
        "start_date": date(2024, 5, 1),
        "last_seen": datetime(2024, 5, 1, 9, 15),
    }
    result = backend.apply_changes(sample_form, ChangeSet(inserts=[RowInsert(values, "New row 1")]), admin)
    assert (result.inserted, result.updated, result.deleted, result.conflicts, result.errors) == (
        1,
        0,
        0,
        [],
        [],
    )
    assert result.ok and result.applied == 1
    assert backend.count_rows(sample_form) == 5
    row = _row(backend.read_rows(sample_form), "E005")
    assert row[CREATED_AT_COLUMN] == pd.Timestamp(T1) and row[UPDATED_AT_COLUMN] == pd.Timestamp(T1)
    assert row[CREATED_BY_COLUMN] == admin.username and row[UPDATED_BY_COLUMN] == admin.username
    assert row[ID_COLUMN] not in {"row-0001", "row-0002", "row-0003", "row-0004"}
    assert row["category"] == "Service" and int(row["qty"]) == 7
    assert float(row["price"]) == pytest.approx(12.34) and float(row["ratio"]) == 0.75
    assert bool(row["active"]) is False
    assert row["start_date"] == date(2024, 5, 1)
    assert row["last_seen"] == pd.Timestamp("2024-05-01 09:15:00")
    assert int(row[VERSION_COLUMN]) == 1


def test_apply_changes_insert_with_numpy_values_and_missing_columns(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    import numpy as np

    result = backend.apply_changes(
        sample_form,
        ChangeSet(
            inserts=[
                RowInsert(
                    {"code": "N001", "qty": np.int64(3), "ratio": np.float64("nan"), "active": np.bool_(True)}
                )
            ]
        ),
        admin,
    )
    assert result.inserted == 1
    row = _row(backend.read_rows(sample_form), "N001")
    assert int(row["qty"]) == 3 and pd.isna(row["ratio"]) and bool(row["active"]) is True
    assert row["category"] is None and pd.isna(row["price"])


def test_apply_changes_update_and_delete_with_matching_token(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    before = backend.read_rows(sample_form)
    a, b = _row(before, "A001"), _row(before, "B002")
    _freeze(monkeypatch, T2)
    other = User("bob@example.org")
    result = backend.apply_changes(
        sample_form,
        ChangeSet(
            updates=[
                RowUpdate(a[ID_COLUMN], {"qty": 11, "category": "Service"}, _version(a), "Row code=A001")
            ],
            deletes=[RowDelete(b[ID_COLUMN], _version(b), "Row code=B002")],
        ),
        other,
    )
    assert (result.inserted, result.updated, result.deleted) == (0, 1, 1)
    assert result.ok and result.summary() == "1 updated, 1 deleted"
    after = backend.read_rows(sample_form)
    assert after["code"].tolist() == ["A001", "C003", "D004"]
    a2 = _row(after, "A001")
    assert int(a2["qty"]) == 11 and a2["category"] == "Service"
    assert int(a2[VERSION_COLUMN]) == 2  # incremented on update
    assert a2[UPDATED_AT_COLUMN] == pd.Timestamp(T2) and a2[UPDATED_BY_COLUMN] == "bob@example.org"
    assert a2[CREATED_AT_COLUMN] == a[CREATED_AT_COLUMN] and a2[CREATED_BY_COLUMN] == admin.username
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    assert form.row_count == 3 and form.updated_by == "bob@example.org" and form.updated_at == T2


def test_apply_changes_update_ignores_system_and_unknown_columns(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    a = _row(backend.read_rows(sample_form), "A001")
    history_before = len(backend.get_history(sample_form))
    result = backend.apply_changes(
        sample_form,
        ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {ID_COLUMN: "hacked", "ghost": 1}, _version(a))]),
        admin,
    )
    assert result.updated == 0 and result.conflicts == [] and result.ok
    assert _row(backend.read_rows(sample_form), "A001")[ID_COLUMN] == "row-0001"
    assert len(backend.get_history(sample_form)) == history_before


def test_apply_changes_stale_token_is_reported_as_conflict(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    a = _row(backend.read_rows(sample_form), "A001")
    stale = _version(a)
    _freeze(monkeypatch, T1)
    assert (
        backend.apply_changes(
            sample_form, ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {"qty": 1}, stale)]), User("carol")
        ).updated
        == 1
    )
    result = backend.apply_changes(
        sample_form, ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {"qty": 2}, stale, "Row code=A001")]), admin
    )
    assert result.updated == 0 and not result.ok
    assert result.conflicts == [
        "Row code=A001: modified by carol at 2024-01-02 12:00:00 UTC after you loaded it."
    ]
    assert result.summary() == "nothing changed; 1 row(s) skipped because they were changed by someone else"
    assert int(_row(backend.read_rows(sample_form), "A001")["qty"]) == 1
    # a stale delete is a conflict too, and the label falls back to the row id
    result = backend.apply_changes(sample_form, ChangeSet(deletes=[RowDelete(a[ID_COLUMN], stale)]), admin)
    assert result.deleted == 0 and result.conflicts[0].startswith("row-0001: modified by carol")
    assert backend.count_rows(sample_form) == 4


def test_apply_changes_none_token_conflicts_with_stamped_row(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    a = _row(backend.read_rows(sample_form), "A001")
    result = backend.apply_changes(
        sample_form, ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {"qty": 1}, None, "A")]), admin
    )
    assert result.updated == 0 and len(result.conflicts) == 1


def test_apply_changes_deleted_row_is_reported_as_conflict(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    b = _row(backend.read_rows(sample_form), "B002")
    assert (
        backend.apply_changes(
            sample_form, ChangeSet(deletes=[RowDelete(b[ID_COLUMN], _version(b))]), admin
        ).deleted
        == 1
    )
    result = backend.apply_changes(
        sample_form,
        ChangeSet(
            updates=[RowUpdate(b[ID_COLUMN], {"qty": 1}, _version(b), "Row code=B002")],
            deletes=[RowDelete(b[ID_COLUMN], _version(b), "Row code=B002")],
        ),
        admin,
    )
    assert result.applied == 0
    assert result.conflicts == ["Row code=B002: the row was deleted by someone else."] * 2


def test_apply_changes_conflicts_do_not_block_other_rows(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    df = backend.read_rows(sample_form)
    a, c = _row(df, "A001"), _row(df, "C003")
    result = backend.apply_changes(
        sample_form,
        ChangeSet(
            inserts=[RowInsert({"code": "E005"})],
            updates=[
                RowUpdate(a[ID_COLUMN], {"qty": 99}, 999, "stale A"),
                RowUpdate(c[ID_COLUMN], {"qty": 33}, _version(c), "C"),
            ],
            deletes=[RowDelete("no-such-row", None, "ghost")],
        ),
        admin,
    )
    assert (result.inserted, result.updated, result.deleted) == (1, 1, 0)
    assert len(result.conflicts) == 2
    assert result.conflicts[1] == "ghost: the row was deleted by someone else."
    after = backend.read_rows(sample_form)
    assert after["code"].tolist()[-1] == "E005" or "E005" in after["code"].tolist()
    assert int(_row(after, "C003")["qty"]) == 33
    assert int(_row(after, "A001")["qty"]) == 10


def test_apply_changes_not_null_violation_rolls_back_whole_batch(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    a = _row(backend.read_rows(sample_form), "A001")
    history_before = len(backend.get_history(sample_form))
    with pytest.raises((BackendError, duckdb.Error), match="NOT NULL"):
        backend.apply_changes(
            sample_form,
            ChangeSet(
                inserts=[RowInsert({"code": "OK1"}), RowInsert({"code": None, "category": "Hardware"})],
                updates=[RowUpdate(a[ID_COLUMN], {"qty": 555}, _version(a))],
            ),
            admin,
        )
    after = backend.read_rows(sample_form)
    assert after["code"].tolist() == ["A001", "B002", "C003", "D004"]
    assert int(_row(after, "A001")["qty"]) == 10
    assert len(backend.get_history(sample_form)) == history_before
    # the backend is still usable after the rollback
    assert (
        backend.apply_changes(sample_form, ChangeSet(inserts=[RowInsert({"code": "OK2"})]), admin).inserted
        == 1
    )


def test_apply_changes_update_to_null_on_required_column_rolls_back(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    a = _row(backend.read_rows(sample_form), "A001")
    with pytest.raises((BackendError, duckdb.Error)):
        backend.apply_changes(
            sample_form, ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {"code": None}, _version(a))]), admin
        )
    assert _row(backend.read_rows(sample_form), "A001")[UPDATED_AT_COLUMN] == a[UPDATED_AT_COLUMN]


def test_apply_changes_empty_changeset(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    result = backend.apply_changes(sample_form, ChangeSet(), admin)
    assert result.ok and result.applied == 0
    assert backend.get_history(sample_form).shape[0] == 4


# ======================================================================================
# append_rows
# ======================================================================================


def test_append_rows_fills_missing_columns_and_parses_date_strings(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    _freeze(monkeypatch, T2)
    other = User("importer@example.org")
    rows = pd.DataFrame(
        {
            "code": ["X1", "X2"],
            "start_date": ["2024-05-01", None],
            "last_seen": ["2024-05-01T10:30:00", "2024-06-01 08:00:00"],
            "ignored_extra": ["a", "b"],
        }
    )
    assert backend.append_rows(sample_form, rows, other) == 2
    df = backend.read_rows(sample_form)
    assert backend.count_rows(sample_form) == 6
    x1, x2 = _row(df, "X1"), _row(df, "X2")
    assert x1["start_date"] == date(2024, 5, 1) and pd.isna(x2["start_date"])
    assert x1["last_seen"] == pd.Timestamp("2024-05-01 10:30:00")
    assert x2["last_seen"] == pd.Timestamp("2024-06-01 08:00:00")
    assert x1["category"] is None and pd.isna(x1["qty"]) and pd.isna(x1["active"])
    assert x1[CREATED_BY_COLUMN] == x1[UPDATED_BY_COLUMN] == "importer@example.org"
    assert x1[CREATED_AT_COLUMN] == x1[UPDATED_AT_COLUMN] == pd.Timestamp(T2)
    assert "ignored_extra" not in df.columns
    history = backend.get_history(sample_form)
    assert history["change_type"].tolist()[:2] == ["insert", "insert"]
    assert set(history["code"].tolist()[:2]) == {"X1", "X2"}
    assert history["changed_by"].tolist()[:2] == ["importer@example.org"] * 2


def test_append_rows_empty_frame_inserts_nothing(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    assert backend.append_rows(sample_form, pd.DataFrame(), admin) == 0
    assert backend.append_rows(sample_form, pd.DataFrame({"code": []}), admin) == 0
    assert backend.count_rows(sample_form) == 4


def test_append_rows_not_null_violation_is_backend_error_and_rolls_back(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    rows = pd.DataFrame({"code": ["OK", None], "qty": [1, 2]})
    with pytest.raises(BackendError, match="Import failed"):
        backend.append_rows(sample_form, rows, admin)
    assert backend.count_rows(sample_form) == 4
    assert len(backend.get_history(sample_form)) == 4


# ======================================================================================
# get_history
# ======================================================================================


def test_get_history_newest_first_with_change_types_and_images(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch
):
    df = backend.read_rows(sample_form)
    a, b = _row(df, "A001"), _row(df, "B002")
    _freeze(monkeypatch, T1, T2)
    backend.apply_changes(
        sample_form,
        ChangeSet(
            updates=[RowUpdate(a[ID_COLUMN], {"qty": 11, "ratio": 0.9, "category": "Hardware"}, _version(a))]
        ),
        User("upd"),
    )
    backend.apply_changes(sample_form, ChangeSet(deletes=[RowDelete(b[ID_COLUMN], _version(b))]), User("del"))
    history = backend.get_history(sample_form)
    assert history.columns.tolist() == [
        *HISTORY_COLUMNS,
        "changed_fields",
        ID_COLUMN,
        *[c.name for c in sample_form.user_columns],
    ]
    assert len(history) == 6  # 4 inserts + update + delete
    assert history["version"].is_monotonic_decreasing
    assert history["change_type"].tolist() == ["delete", "update", "insert", "insert", "insert", "insert"]
    assert history["changed_by"].tolist()[:2] == ["del", "upd"]
    assert history["changed_at"].tolist()[:2] == [T2, T1]
    delete_row = history.iloc[0]
    assert delete_row[ID_COLUMN] == b[ID_COLUMN]
    assert delete_row["code"] == "B002" and delete_row["category"] == "Software"  # before image
    assert delete_row["changed_fields"] == ""
    update_row = history.iloc[1]
    assert update_row[ID_COLUMN] == a[ID_COLUMN]
    assert update_row["changed_fields"] == "qty, ratio"  # category unchanged
    assert update_row["qty"] == 11 and update_row["code"] == "A001"
    inserts = history.iloc[2:]
    assert set(inserts["code"]) == {"A001", "B002", "C003", "D004"}
    assert inserts["changed_fields"].tolist() == [""] * 4
    assert inserts["changed_by"].tolist() == [admin.username] * 4
    assert len(backend.get_history(sample_form, limit=2)) == 2
    assert backend.get_history(sample_form, limit=2)["change_type"].tolist() == ["delete", "update"]


def test_get_history_for_form_without_changes(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("dom"), admin)
    form = backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("code")]), admin)
    history = backend.get_history(form)
    assert history.empty
    assert history.columns.tolist() == [*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, "code"]


# ======================================================================================
# Schema changes
# ======================================================================================


def test_add_column_persists_description_and_config(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    new = ColumnDef("colour", DataType.STRING, "Colour", nullable=False, options=["Red", "Blue"])
    form = backend.add_column(sample_form, new, admin)
    col = form.column("colour")
    assert [c.name for c in form.columns][-1] == "colour"
    assert col.nullable is True  # existing rows have no value: forced nullable
    assert col.description == "Colour"
    assert col.options == ["Red", "Blue"]
    assert form.properties[PROP_COLUMN_CONFIG] == form.column_config_json()
    assert form.column("code").is_key  # existing config preserved
    df = backend.read_rows(form)
    assert df["colour"].tolist() == [None] * 4
    assert "colour" in backend.get_history(form).columns


def test_add_column_of_each_type(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    form = sample_form
    for i, t in enumerate(DataType.editable_types()):
        form = backend.add_column(form, ColumnDef(f"extra_{i}", t, precision=6, scale=1), admin)
    assert form.column("extra_2").native_type == "DECIMAL(6,1)"
    assert {c.data_type for c in form.user_columns} == set(DataType.editable_types())


def test_add_column_errors(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    with pytest.raises(ConflictError, match="already exists"):
        backend.add_column(sample_form, ColumnDef("code"), admin)
    with pytest.raises(BackendError, match="System columns"):
        backend.add_column(sample_form, ColumnDef(ID_COLUMN), admin)
    with pytest.raises(ValueError):
        backend.add_column(sample_form, ColumnDef("Bad Name"), admin)
    with pytest.raises(ValueError, match="only supported for text"):
        backend.add_column(sample_form, ColumnDef("n", DataType.INTEGER, options=["1"]), admin)
    assert len(backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM).columns) == len(sample_form.columns)


def test_drop_column_removes_column_and_config(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    form = backend.drop_column(sample_form, "category", admin)
    assert form.column("category") is None
    assert "category" not in backend.read_rows(form).columns
    assert "category" not in form.properties[PROP_COLUMN_CONFIG]
    form = backend.drop_column(form, "code", admin)  # dropping the key column clears the key entry
    assert form.column_config()["columns"] == {}
    assert form.properties[PROP_COLUMN_CONFIG] == '{"columns":{},"version":1}'
    assert form.row_count == 4


@pytest.mark.parametrize("name", SYSTEM_COLUMNS)
def test_drop_column_protects_system_columns(
    backend: DuckDBBackend, admin: User, sample_form: FormDef, name: str
):
    with pytest.raises(BackendError, match="System columns cannot be removed"):
        backend.drop_column(sample_form, name, admin)


def test_drop_column_missing(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    with pytest.raises(NotFoundError, match="Column 'ghost' does not exist"):
        backend.drop_column(sample_form, "ghost", admin)


def test_update_form_metadata_persists_descriptions_owner_and_config(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    form.description = "New description"
    form.display_name = "Product Catalogue"
    form.owner = "new.owner@example.org"
    form.column("qty").description = "Stock level"
    form.column("category").options = ["Hardware", "Software"]
    form.column("category").is_key = True
    form.column("code").is_key = False
    updated = backend.update_form_metadata(form, admin)
    assert updated.description == "New description"
    assert updated.display_name == "Product Catalogue" and updated.title == "Product Catalogue"
    assert updated.owner == "new.owner@example.org"
    assert updated.tags["rdm_display_name"] == "Product Catalogue"
    assert updated.tags["rdm_owner"] == "new.owner@example.org"
    assert updated.column("qty").description == "Stock level"
    assert updated.column("category").options == ["Hardware", "Software"]
    assert [c.name for c in updated.key_columns] == ["category"]
    assert updated.properties["created_by"] == admin.username  # untouched
    listed = backend.list_forms(SAMPLE_FUNCTION)[0]
    assert listed.description == "New description" and listed.display_name == "Product Catalogue"


def test_update_form_metadata_toggles_not_null(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    form.column("code").nullable = True
    relaxed = backend.update_form_metadata(form, admin)
    assert relaxed.column("code").nullable is True
    # now nulls are allowed on code
    assert (
        backend.apply_changes(
            relaxed, ChangeSet(inserts=[RowInsert({"category": "Hardware"})]), admin
        ).inserted
        == 1
    )
    relaxed.column("code").nullable = False
    with pytest.raises(BackendError, match="Cannot make 'code' required"):
        backend.update_form_metadata(relaxed, admin)


def test_update_form_metadata_not_null_failure_rolls_back_everything(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    form.description = "should not persist"
    form.column("category").nullable = False  # D004 has no category
    with pytest.raises(BackendError, match="Cannot make 'category' required"):
        backend.update_form_metadata(form, admin)
    fresh = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    assert fresh.description == "Sample products"
    assert fresh.column("category").nullable is True


def test_update_form_metadata_set_not_null_when_no_nulls(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    c = _row(backend.read_rows(sample_form), "C003")
    backend.apply_changes(
        sample_form, ChangeSet(updates=[RowUpdate(c[ID_COLUMN], {"qty": 30}, _version(c))]), admin
    )
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    form.column("qty").nullable = False
    updated = backend.update_form_metadata(form, admin)
    assert updated.column("qty").nullable is False and updated.column("qty").required
    with pytest.raises((BackendError, duckdb.Error)):
        backend.apply_changes(updated, ChangeSet(inserts=[RowInsert({"code": "Z"})]), admin)


def test_update_form_metadata_errors(backend: DuckDBBackend, admin: User, sample_form: FormDef):
    form = backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    form.columns.append(ColumnDef("ghost"))
    with pytest.raises(NotFoundError, match="Column 'ghost' does not exist"):
        backend.update_form_metadata(form, admin)
    with pytest.raises(NotFoundError, match="does not exist"):
        backend.update_form_metadata(FormDef(SAMPLE_FUNCTION, "missing", columns=[ColumnDef("x")]), admin)
    with pytest.raises(ValueError):
        backend.update_form_metadata(FormDef(SAMPLE_FUNCTION, SAMPLE_FORM), admin)


def test_drop_form_removes_the_table_and_its_metadata_but_keeps_the_audit_trail(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    """The audit trail outlives the form (FUNCTIONAL_DESIGN 7.5), as it does on Databricks."""
    assert _meta_count(backend, "object_properties", table_name=SAMPLE_FORM) > 0
    assert _meta_count(backend, "change_log", table_name=SAMPLE_FORM) == 4
    backend.drop_form(sample_form, admin)
    with pytest.raises(NotFoundError):
        backend.get_form(SAMPLE_FUNCTION, SAMPLE_FORM)
    assert backend.list_forms(SAMPLE_FUNCTION) == []
    assert backend.get_function(SAMPLE_FUNCTION).form_count == 0
    assert _meta_count(backend, "object_properties", table_name=SAMPLE_FORM) == 0
    assert _meta_count(backend, "change_log", table_name=SAMPLE_FORM) == 4
    with pytest.raises(NotFoundError):
        backend.drop_form(sample_form, admin)
    # The name can be reused; the table starts empty, and because objects are keyed by name
    # the recreated form inherits the trail of its predecessor (docs/DATA_MODEL.md 3).
    recreated = backend.create_form(FormDef(SAMPLE_FUNCTION, SAMPLE_FORM, columns=[ColumnDef("only")]), admin)
    assert (
        recreated.row_count == 0
        and recreated.display_name == ""
        and [c.name for c in recreated.user_columns] == ["only"]
    )
    assert _meta_count(backend, "change_log", table_name=SAMPLE_FORM) == 4


# ======================================================================================
# Permissions and grants
# ======================================================================================


def _functions(backend: DuckDBBackend, admin: User, *names: str) -> None:
    for n in names:
        backend.create_function(FunctionDef(n), admin)


def test_get_permissions_without_grants(backend: DuckDBBackend, admin: User):
    _functions(backend, admin, "a", "b")
    perms = backend.get_permissions(User("nobody", groups=("strangers",)))
    assert perms.function_roles == {"a": Role.NONE, "b": Role.NONE}
    assert perms.visible_functions == [] and not perms.can_create_function and not perms.is_admin_anywhere


def test_get_permissions_catalog_level_grant_applies_to_every_domain(backend: DuckDBBackend, admin: User):
    _functions(backend, admin, "a", "b")
    backend.grant_function_role(CATALOG_LEVEL, "readers", Role.VIEWER, admin)
    perms = backend.get_permissions(User("u", groups=("readers",)))
    assert perms.function_roles == {"a": Role.VIEWER, "b": Role.VIEWER}
    assert not perms.can_create_function
    backend.create_function(FunctionDef("c"), admin)
    assert backend.get_permissions(User("u", groups=("readers",))).role_for("c") is Role.VIEWER
    backend.grant_function_role(CATALOG_LEVEL, "u", Role.ADMIN, admin)
    perms = backend.get_permissions(User("u", groups=("readers",)))
    assert perms.function_roles == {"a": Role.ADMIN, "b": Role.ADMIN, "c": Role.ADMIN}
    assert perms.can_create_function and perms.admin_functions == ["a", "b", "c"]


def test_get_permissions_takes_the_max_of_all_matching_grants(backend: DuckDBBackend, admin: User):
    _functions(backend, admin, "a", "b")
    backend.grant_function_role("a", "grp_view", Role.VIEWER, admin)
    backend.grant_function_role("a", "grp_edit", Role.EDITOR, admin)
    backend.grant_function_role("b", "grp_view", Role.ADMIN, admin)
    backend.grant_function_role(CATALOG_LEVEL, "grp_view", Role.VIEWER, admin)
    user = User("u", groups=("grp_view", "grp_edit"))
    perms = backend.get_permissions(user)
    assert (
        perms.role_for("a") is Role.EDITOR
    )  # max(VIEWER via grp_view, EDITOR via grp_edit, VIEWER via catalog)
    assert perms.role_for("b") is Role.ADMIN
    assert not perms.is_global_admin
    # a lower domain-level grant never reduces a higher catalog-level one
    backend.grant_function_role(CATALOG_LEVEL, "grp_edit", Role.ADMIN, admin)
    perms = backend.get_permissions(user)
    assert perms.role_for("a") is Role.ADMIN and perms.is_global_admin


def test_get_permissions_matches_username_email_and_groups_separately(backend: DuckDBBackend, admin: User):
    """Grants to individuals cannot be created through the app, but ones that exist still count."""
    _functions(backend, admin, "a")
    backend._conn.execute("INSERT INTO _catalog.grants VALUES ('a', 'mail@example.org', 'VIEWER')")
    assert backend.get_permissions(User("someone", email="mail@example.org")).role_for("a") is Role.VIEWER
    assert backend.get_permissions(User("mail@example.org")).role_for("a") is Role.VIEWER
    assert backend.get_permissions(User("other", groups=("mail@example.org",))).role_for("a") is Role.VIEWER
    assert backend.get_permissions(User("other")).role_for("a") is Role.NONE
    assert "mail@example.org" not in backend.list_groups()


def test_grant_replace_and_revoke(backend: DuckDBBackend, admin: User):
    _functions(backend, admin, "a")
    backend.grant_function_role("a", "zeta", Role.EDITOR, admin)
    backend.grant_function_role("a", "  beta  ", Role.VIEWER, admin)
    assert backend.list_function_grants("a") == [("beta", Role.VIEWER), ("zeta", Role.EDITOR)]
    assert {"beta", "zeta"} <= set(backend.list_groups()) and backend.list_groups("ZET") == ["zeta"]
    backend.grant_function_role("a", "zeta", Role.VIEWER, admin)  # replaces
    assert dict(backend.list_function_grants("a"))["zeta"] is Role.VIEWER
    backend.grant_function_role("a", "zeta", Role.NONE, admin)  # revokes
    assert [p for p, _ in backend.list_function_grants("a")] == ["beta"]
    assert backend.get_permissions(User("zeta")).role_for("a") is Role.NONE
    backend.grant_function_role("a", "beta", Role.NONE, admin)
    backend.grant_function_role("a", "beta", Role.NONE, admin)  # revoking twice is harmless
    assert backend.list_function_grants("a") == []
    assert backend.list_function_grants("never_granted") == []


def test_grant_errors(backend: DuckDBBackend, admin: User):
    with pytest.raises(NotFoundError, match="Function 'ghost' does not exist"):
        backend.grant_function_role("ghost", "grp", Role.VIEWER, admin)
    _functions(backend, admin, "a")
    for bad in ("", "   "):
        with pytest.raises(BackendError, match="group is required"):
            backend.grant_function_role("a", bad, Role.VIEWER, admin)
    with pytest.raises(BackendError, match="groups only"):
        backend.grant_function_role("a", "someone@example.org", Role.VIEWER, admin)
    assert backend.list_function_grants("a") == []


def test_file_backend_persists_across_close_and_reopen(tmp_path, admin: User):
    path = tmp_path / "nested" / "dir" / "rdm.duckdb"
    b = DuckDBBackend(str(path))
    try:
        assert path.exists() and b.describe() == f"DuckDB ({path})"
        b.create_function(FunctionDef("dom", display_name="Dom", description="persisted"), admin)
        form = b.create_form(
            FormDef("dom", "frm", display_name="Frm", columns=sample_columns()), admin, sample_rows()
        )
        b.grant_function_role("dom", "readers", Role.VIEWER, admin)
        row = _row(b.read_rows(form), "A001")
        b.apply_changes(
            form, ChangeSet(updates=[RowUpdate(row[ID_COLUMN], {"qty": 12}, _version(row))]), admin
        )
    finally:
        b.close()

    reopened = DuckDBBackend(str(path))
    try:
        assert [d.name for d in reopened.list_functions()] == ["dom"]
        domain = reopened.get_function("dom")
        assert domain.display_name == "Dom" and domain.description == "persisted" and domain.form_count == 1
        form = reopened.get_form("dom", "frm")
        assert form.row_count == 4 and form.display_name == "Frm"
        assert form.column("code").is_key and form.column("category").options == [
            "Hardware",
            "Software",
            "Service",
        ]
        df = reopened.read_rows(form)
        assert sorted(df["code"].tolist()) == ["A001", "B002", "C003", "D004"]
        assert int(_row(df, "A001")["qty"]) == 12
        history = reopened.get_history(form)
        assert history["change_type"].tolist() == ["update", "insert", "insert", "insert", "insert"]
        assert reopened.list_function_grants("dom") == [("readers", Role.VIEWER)]
        assert reopened.get_permissions(User("x", groups=("readers",))).role_for("dom") is Role.VIEWER
        # the meta schema is created idempotently
        assert (
            reopened.apply_changes(form, ChangeSet(inserts=[RowInsert({"code": "E005"})]), admin).inserted
            == 1
        )
    finally:
        reopened.close()


def test_close_is_idempotent(backend: DuckDBBackend):
    backend.close()
    backend.close()


def test_seeded_backend_contains_demo_content(seeded_backend: DuckDBBackend):
    domains = {d.name: d for d in seeded_backend.list_domains()}
    assert {n: d.function_count for n, d in domains.items()} == {
        "customer": 1,
        "finance": 1,
        "people": 1,
        "research": 0,
    }
    functions = {f.name: f for f in seeded_backend.list_functions()}
    assert set(functions) == {
        "customer__survey_service_improvement",
        "finance__cost_management",
        "hr__reference",
    }
    assert functions["finance__cost_management"].form_count == 2
    assert functions["finance__cost_management"].file_count == 2
    files = {f.name: f for f in seeded_backend.list_files("finance__cost_management")}
    assert files["gl_transactions.csv"].row_count == 2000 and files["gl_transactions.csv"].registered
    assert files["fx_rates.parquet"].row_count == 366 and files["fx_rates.parquet"].title == "FX Rates"
    assert seeded_backend.preview_file(files["fx_rates.parquet"], limit=3).columns.tolist() == [
        "rate_date",
        "currency",
        "rate_to_gbp",
    ]
    assert functions["finance__cost_management"].domain == "finance"
    assert functions["hr__reference"].display_name == "HR Reference"
    assert functions["hr__reference"].domain == "people"
    forms = {(f.function, f.name): f for d in functions for f in seeded_backend.list_forms(d)}
    assert set(forms) == {
        ("customer__survey_service_improvement", "survey_questions"),
        ("customer__survey_service_improvement", "service_areas"),
        ("finance__cost_management", "cost_centres"),
        ("finance__cost_management", "gl_account_mappings"),
        ("hr__reference", "employment_types"),
    }
    questions = seeded_backend.get_form("customer__survey_service_improvement", "survey_questions")
    assert questions.row_count == 6
    assert questions.column("category").options == [
        "Product",
        "Delivery",
        "Support",
        "Billing",
        "Overall",
    ]
    assert [c.name for c in questions.key_columns] == ["question_code"]
    grants = dict(seeded_backend.list_function_grants("finance__cost_management"))
    assert grants["finance_stewards"] is Role.EDITOR and grants["finance_readers"] is Role.VIEWER
    assert grants["finance_admins"] is Role.ADMIN
    assert dict(seeded_backend.list_function_grants(CATALOG_LEVEL)) == {"rdm_admins": Role.ADMIN}


# ======================================================================================
# Domains (classifier), drop_function, per-row history, meta migration
# ======================================================================================


def test_domain_crud_and_function_assignment(backend: DuckDBBackend, admin: User):
    assert backend.list_domains() == []
    created = backend.create_domain(DomainDef("customer", "Customer", "Customer data", ""), admin)
    assert (created.name, created.display_name, created.description, created.function_count) == (
        "customer",
        "Customer",
        "Customer data",
        0,
    )
    assert created.owner == admin.username  # defaults to the actor
    backend.create_domain(DomainDef("finance", "Finance"), admin)
    assert [d.name for d in backend.list_domains()] == ["customer", "finance"]
    with pytest.raises(ConflictError, match="already exists"):
        backend.create_domain(DomainDef("customer"), admin)
    with pytest.raises(ValueError):
        backend.create_domain(DomainDef("Bad Name"), admin)
    with pytest.raises(NotFoundError, match="Domain 'ghost' does not exist"):
        backend.get_domain("ghost")
    with pytest.raises(NotFoundError):
        backend.update_domain(DomainDef("ghost"), admin)
    updated = backend.update_domain(
        DomainDef("customer", "Customers", "All customer lists", "s@example.org"), admin
    )
    assert (updated.display_name, updated.description, updated.owner) == (
        "Customers",
        "All customer lists",
        "s@example.org",
    )
    # assigning a function to a domain
    with pytest.raises(NotFoundError, match="Domain 'ghost' does not exist"):
        backend.create_function(FunctionDef("survey", domain="ghost"), admin)
    f = backend.create_function(FunctionDef("survey", domain="customer"), admin)
    assert f.domain == "customer" and f.properties["rdm.domain"] == "customer"
    assert backend.get_domain("customer").function_count == 1
    assert _meta_count(backend, "functions", name="survey", domain_name="customer") == 1
    with pytest.raises(ConflictError, match="still has 1 function"):
        backend.delete_domain("customer", admin)
    f = backend.update_function(FunctionDef("survey", domain="finance"), admin)
    assert f.domain == "finance"
    assert {d.name: d.function_count for d in backend.list_domains()} == {"customer": 0, "finance": 1}
    backend.delete_domain("customer", admin)
    assert [d.name for d in backend.list_domains()] == ["finance"]
    with pytest.raises(NotFoundError):
        backend.delete_domain("customer", admin)
    # clearing the assignment
    assert backend.update_function(FunctionDef("survey", domain=""), admin).domain == ""
    assert backend.get_domain("finance").function_count == 0


def test_drop_function_requires_an_empty_schema_and_cleans_up(
    backend: DuckDBBackend, admin: User, sample_form: FormDef
):
    function = backend.get_function(SAMPLE_FUNCTION)
    backend.grant_function_role(SAMPLE_FUNCTION, "readers", Role.VIEWER, admin)
    with pytest.raises(ConflictError, match="still has 1 form"):
        backend.drop_function(function, admin)
    backend.drop_form(sample_form, admin)
    backend.drop_function(function, admin)
    with pytest.raises(NotFoundError):
        backend.get_function(SAMPLE_FUNCTION)
    assert backend.list_functions() == []
    assert backend.list_function_grants(SAMPLE_FUNCTION) == []
    assert _meta_count(backend, "functions", name=SAMPLE_FUNCTION) == 0
    assert _meta_count(backend, "object_properties", schema_name=SAMPLE_FUNCTION) == 0
    with pytest.raises(NotFoundError):
        backend.drop_function(function, admin)
    assert backend.get_permissions(User("x", groups=("readers",))).function_roles == {}


def test_get_history_for_one_row(backend: DuckDBBackend, admin: User, sample_form: FormDef, monkeypatch):
    df = backend.read_rows(sample_form)
    a, b = _row(df, "A001"), _row(df, "B002")
    _freeze(monkeypatch, T1, T2)
    backend.apply_changes(
        sample_form, ChangeSet(updates=[RowUpdate(a[ID_COLUMN], {"qty": 11}, _version(a))]), User("upd")
    )
    backend.apply_changes(sample_form, ChangeSet(deletes=[RowDelete(b[ID_COLUMN], _version(b))]), User("del"))
    history = backend.get_history(sample_form, row_id=a[ID_COLUMN])
    assert history["change_type"].tolist() == ["update", "insert"]
    assert history[ID_COLUMN].tolist() == [a[ID_COLUMN]] * 2
    assert history["qty"].tolist() == [11, 10]
    assert len(backend.get_history(sample_form, limit=1, row_id=a[ID_COLUMN])) == 1
    deleted = backend.get_history(sample_form, row_id=b[ID_COLUMN])
    assert deleted["change_type"].tolist() == ["delete", "insert"] and deleted.iloc[0]["code"] == "B002"
    assert backend.get_history(sample_form, row_id="no-such-row").empty
    assert len(backend.get_history(sample_form)) == 6


def test_meta_migration_from_pre_function_layout(tmp_path, admin: User):
    """A local database from before domains and functions were split is upgraded on open."""
    path = tmp_path / "old.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute('CREATE SCHEMA "_catalog"')
    conn.execute(
        'CREATE TABLE "_catalog"."domains" (name VARCHAR PRIMARY KEY, display_name VARCHAR, description VARCHAR, '
        "owner VARCHAR, doc_link VARCHAR, created_at TIMESTAMP, created_by VARCHAR, updated_at TIMESTAMP, updated_by VARCHAR)"
    )
    conn.execute(
        'CREATE TABLE "_catalog"."forms" (domain VARCHAR NOT NULL, name VARCHAR NOT NULL, display_name VARCHAR, '
        "description VARCHAR, owner VARCHAR, created_at TIMESTAMP, created_by VARCHAR, updated_at TIMESTAMP, "
        "updated_by VARCHAR, PRIMARY KEY (domain, name))"
    )
    conn.execute(
        "INSERT INTO \"_catalog\".\"domains\" VALUES ('finance', 'Finance', 'd', 'o', 'https://x', now(), 'a', now(), 'a')"
    )
    conn.execute(
        "INSERT INTO \"_catalog\".\"forms\" VALUES ('finance', 'frm', 'Frm', '', '', now(), 'a', now(), 'a')"
    )
    conn.execute('CREATE SCHEMA "finance"')
    conn.close()
    b = DuckDBBackend(str(path))
    try:
        assert _meta_count(b, "functions", name="finance") == 1
        assert _meta_count(b, "forms", function_name="finance", name="frm") == 1
        assert b.list_domains() == []  # the new domain list starts empty
        d = b.create_domain(DomainDef("fin", "Finance"), admin)
        assert d.function_count == 0
        assert b.update_function(FunctionDef("finance", domain="fin"), admin).domain == "fin"
    finally:
        b.close()


# ======================================================================================
# Files (CSV / Parquet in the function's files directory)
# ======================================================================================

CSV = b"code,amount,when\nA,1.5,2024-01-01\nB,2.25,2024-02-01\n"


def _parquet(rows: int = 3) -> bytes:
    import io

    buffer = io.BytesIO()
    pd.DataFrame({"x": range(rows), "y": ["v"] * rows}).to_parquet(buffer, index=False)
    return buffer.getvalue()


def test_put_file_stores_registers_and_logs(backend: DuckDBBackend, admin: User, monkeypatch):
    backend.create_function(FunctionDef("finance"), admin)
    assert backend.list_files("finance") == [] and backend.get_function("finance").file_count == 0
    _freeze(monkeypatch, T1)
    f = backend.put_file(FileDef("finance", "gl.csv", display_name="GL", description="d"), CSV, admin)
    assert (f.name, f.format, f.size_bytes, f.row_count) == ("gl.csv", "csv", len(CSV), 2)
    assert f.display_name == "GL" and f.owner == admin.username and f.registered
    assert f.created_by == admin.username and f.created_at == T1 and f.updated_at == T1
    assert f.path.endswith("finance/gl.csv") and backend.read_file(f) == CSV
    assert backend.get_function("finance").file_count == 1
    assert _meta_count(backend, "files", function_name="finance", name="gl.csv") == 1
    history = backend.file_history(f)
    assert history["change_type"].tolist() == ["upload"] and history["row_count"].tolist() == [2]
    assert history["size_bytes"].tolist() == [len(CSV)] and history["changed_by"].tolist() == [admin.username]
    with pytest.raises(ConflictError, match="already exists"):
        backend.put_file(FileDef("finance", "gl.csv"), CSV, admin)
    with pytest.raises(NotFoundError, match="does not exist"):
        backend.put_file(FileDef("finance", "other.csv"), CSV, admin, replace=True)
    with pytest.raises(NotFoundError, match="Function 'ghost' does not exist"):
        backend.put_file(FileDef("ghost", "gl.csv"), CSV, admin)
    with pytest.raises(ValueError):
        backend.put_file(FileDef("finance", "bad name.csv"), CSV, admin)


def test_preview_columns_and_parquet(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    csv = backend.put_file(FileDef("finance", "gl.csv"), CSV, admin)
    preview = backend.preview_file(csv, limit=1)
    assert preview.columns.tolist() == ["code", "amount", "when"] and len(preview) == 1
    assert [(c.name, c.data_type) for c in backend.file_columns(csv)] == [
        ("code", DataType.STRING),
        ("amount", DataType.DOUBLE),
        ("when", DataType.DATE),
    ]
    pq = backend.put_file(FileDef("finance", "rates.parquet"), _parquet(5), admin)
    assert pq.row_count == 5 and pq.format == "parquet"
    assert len(backend.preview_file(pq, limit=10)) == 5
    assert [c.native_type for c in backend.file_columns(pq)] == ["BIGINT", "VARCHAR"]
    assert [f.name for f in backend.list_files("finance")] == ["gl.csv", "rates.parquet"]


def test_replace_file_keeps_metadata_and_history(backend: DuckDBBackend, admin: User, monkeypatch):
    backend.create_function(FunctionDef("finance"), admin)
    _freeze(monkeypatch, T1, T2)
    backend.put_file(
        FileDef("finance", "gl.csv", display_name="GL", description="d", owner="o@x"), CSV, admin
    )
    new = CSV + b"C,3,2024-03-01\n"
    replaced = backend.put_file(FileDef("finance", "gl.csv"), new, User("bob"), replace=True)
    assert (replaced.row_count, replaced.size_bytes) == (3, len(new))
    assert (replaced.display_name, replaced.description, replaced.owner) == ("GL", "d", "o@x")
    assert replaced.created_by == admin.username and replaced.updated_by == "bob"
    assert backend.read_file(replaced) == new
    history = backend.file_history(replaced)
    assert history["change_type"].tolist() == ["replace", "upload"]
    assert history["row_count"].tolist() == [3, 2] and history["changed_by"].tolist() == [
        "bob",
        admin.username,
    ]
    assert len(backend.file_history(replaced, limit=1)) == 1


def test_unreadable_upload_is_rejected_and_previous_content_kept(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    with pytest.raises(BackendError, match="cannot be read as PARQUET"):
        backend.put_file(FileDef("finance", "bad.parquet"), b"garbage", admin)
    assert backend.list_files("finance") == []
    assert _meta_count(backend, "files", function_name="finance") == 0
    backend.put_file(FileDef("finance", "ok.parquet"), _parquet(), admin)
    with pytest.raises(BackendError, match="cannot be read"):
        backend.put_file(FileDef("finance", "ok.parquet"), b"garbage", admin, replace=True)
    assert backend.get_file("finance", "ok.parquet").row_count == 3
    assert backend.file_history(FileDef("finance", "ok.parquet"))["change_type"].tolist() == ["upload"]


def test_update_file_metadata_and_unregistered_files(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    f = backend.put_file(FileDef("finance", "gl.csv"), CSV, admin)
    f.display_name, f.description, f.owner = "General Ledger", "postings", "fin@x"
    updated = backend.update_file_metadata(f, User("bob"))
    assert (updated.display_name, updated.description, updated.owner) == (
        "General Ledger",
        "postings",
        "fin@x",
    )
    assert updated.updated_by == "bob" and updated.row_count == 2  # counts untouched
    # a file landed outside the app (pipeline / CLI) shows up unregistered, other types are ignored
    folder = backend.files_dir / "finance"
    (folder / "landed.parquet").write_bytes(_parquet(4))
    (folder / "notes.txt").write_text("ignored")
    names = {x.name: x for x in backend.list_files("finance")}
    assert set(names) == {"gl.csv", "landed.parquet"}
    landed = names["landed.parquet"]
    assert not landed.registered and landed.row_count is None and landed.size_bytes == len(_parquet(4))
    assert backend.get_function("finance").file_count == 2
    assert len(backend.preview_file(landed)) == 4
    landed.description = "from the pipeline"
    assert backend.update_file_metadata(landed, admin).registered
    with pytest.raises(NotFoundError):
        backend.get_file("finance", "missing.csv")
    with pytest.raises(NotFoundError):
        backend.update_file_metadata(FileDef("finance", "missing.csv"), admin)
    with pytest.raises(NotFoundError):
        backend.list_files("ghost")


def test_drop_file_and_drop_function_guard(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    f = backend.put_file(FileDef("finance", "gl.csv"), CSV, admin)
    function = backend.get_function("finance")
    with pytest.raises(ConflictError, match="still has 1 file"):
        backend.drop_function(function, admin)
    backend.drop_file(f, User("del"))
    assert backend.list_files("finance") == []
    assert _meta_count(backend, "files", function_name="finance") == 0
    history = backend.file_history(f)
    assert history["change_type"].tolist() == ["delete", "upload"]
    assert history.iloc[0]["changed_by"] == "del" and history.iloc[0]["row_count"] == 2
    with pytest.raises(NotFoundError):
        backend.drop_file(f, admin)
    with pytest.raises(NotFoundError):
        backend.read_file(f)
    backend.drop_function(function, admin)
    assert not (backend.files_dir / "finance").exists()


def test_drop_function_never_deletes_unknown_content_in_the_files_folder(backend: DuckDBBackend, admin: User):
    backend.create_function(FunctionDef("finance"), admin)
    folder = backend.files_dir / "finance"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "notes.txt").write_text("not managed by the app")
    (folder / "archive").mkdir()
    assert backend.list_files("finance") == []  # ignored by the app ...
    with pytest.raises(ConflictError, match="still has 2 file\\(s\\) or folder"):
        backend.drop_function(backend.get_function("finance"), admin)  # ... but never deleted by it
    assert (folder / "notes.txt").read_text() == "not managed by the app"
    assert backend.get_function("finance").name == "finance"
    (folder / "notes.txt").unlink()
    (folder / "archive").rmdir()
    backend.drop_function(backend.get_function("finance"), admin)
    assert not folder.exists()


def test_files_persist_next_to_the_database(tmp_path, admin: User):
    path = tmp_path / "db" / "rdm.duckdb"
    b = DuckDBBackend(str(path))
    try:
        assert b.files_dir == tmp_path / "db" / "files"
        b.create_function(FunctionDef("fin"), admin)
        b.put_file(FileDef("fin", "gl.csv", display_name="GL"), CSV, admin)
    finally:
        b.close()
    assert (tmp_path / "db" / "files" / "fin" / "gl.csv").read_bytes() == CSV
    reopened = DuckDBBackend(str(path))
    try:
        [f] = reopened.list_files("fin")
        assert f.display_name == "GL" and f.row_count == 2 and f.registered
    finally:
        reopened.close()
    custom = DuckDBBackend(files_dir=str(tmp_path / "elsewhere"))
    try:
        custom.create_function(FunctionDef("fin"), admin)
        custom.put_file(FileDef("fin", "gl.csv"), CSV, admin)
        assert (tmp_path / "elsewhere" / "fin" / "gl.csv").exists()
    finally:
        custom.close()
    assert (tmp_path / "elsewhere" / "fin" / "gl.csv").exists()  # only temp dirs are removed on close


def test_owner_email_roundtrip_for_function_form_and_file(backend, admin):
    backend.create_function(
        FunctionDef("crm", description="CRM lists", owner="crm team", owner_email="crm@example.org"),
        admin,
    )
    assert backend.get_function("crm").owner_email == "crm@example.org"
    form = backend.create_form(
        FormDef(
            "crm",
            "accounts",
            description="Accounts",
            owner="crm team",
            owner_email="accounts@example.org",
            columns=[ColumnDef("code")],
        ),
        admin,
    )
    assert form.owner_email == "accounts@example.org"
    assert [f.owner_email for f in backend.list_forms("crm")] == ["accounts@example.org"]
    form.owner_email = ""
    assert backend.update_form_metadata(form, admin).owner_email == ""
    file = backend.put_file(
        FileDef("crm", "leads.csv", description="Leads", owner="crm", owner_email="leads@example.org"),
        b"a,b\n1,2\n",
        admin,
    )
    assert file.owner_email == "leads@example.org"
    file.owner_email = "sales@example.org"
    assert backend.update_file_metadata(file, admin).owner_email == "sales@example.org"
    # a replacement without metadata keeps the previous e-mail
    replaced = backend.put_file(FileDef("crm", "leads.csv"), b"a,b\n3,4\n", admin, replace=True)
    assert replaced.owner_email == "sales@example.org"


def test_scd2_history_windows_follow_the_row_lifecycle(backend, admin):
    """FR-47: __START_AT/__END_AT windows in the organisation's Auto CDC notation."""
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(
        FormDef(
            "crm",
            "accounts",
            scd2_enabled=True,
            columns=[ColumnDef("code", DataType.STRING, is_key=True), ColumnDef("name")],
        ),
        admin,
        rows=pd.DataFrame({"code": ["A"], "name": ["Alpha"]}),
    )
    assert form.scd2_enabled
    hist = 'crm."_h__accounts"'
    con = backend._conn  # noqa: SLF001 - white-box check of the history table
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 1
    rows = backend.read_rows(form)
    rid = str(rows.iloc[0]["_id"])
    # update: the old window closes, a new one opens with the new value
    backend.apply_changes(form, ChangeSet(updates=[RowUpdate(rid, {"name": "Alpha 2"}, 1)]), admin)
    open_rows = con.execute(f"SELECT name FROM {hist} WHERE __END_AT IS NULL").fetchall()
    assert open_rows == [("Alpha 2",)]
    assert con.execute(f"SELECT count(*) FROM {hist}").fetchone()[0] == 2
    # delete: the window closes, nothing opens
    backend.apply_changes(form, ChangeSet(deletes=[RowDelete(rid, 2)]), admin)
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 0
    # append feeds history; a conflicting update does not touch it
    backend.append_rows(form, pd.DataFrame({"code": ["B"], "name": ["Beta"]}), admin)
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 1
    stale = backend.apply_changes(
        form, ChangeSet(updates=[RowUpdate(rid, {"name": "ghost"}, 1)]), admin
    )
    assert stale.conflicts and con.execute(f"SELECT count(*) FROM {hist}").fetchone()[0] == 3


def test_scd2_toggle_backfills_and_stops_maintenance(backend, admin):
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(
        FormDef("crm", "leads", columns=[ColumnDef("code")]),
        admin,
        rows=pd.DataFrame({"code": ["A", "B"]}),
    )
    assert not form.scd2_enabled
    form = backend.set_scd2(form, True, admin)
    assert form.scd2_enabled
    con = backend._conn  # noqa: SLF001 - white-box check of the history table
    hist = 'crm."_h__leads"'
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 2
    # history tables never appear as forms and do not count towards the function
    assert [f.name for f in backend.list_forms("crm")] == ["leads"]
    assert backend.get_function("crm").form_count == 1
    # disabling stops maintenance but keeps the table
    form = backend.set_scd2(form, False, admin)
    backend.append_rows(form, pd.DataFrame({"code": ["C"]}), admin)
    assert con.execute(f"SELECT count(*) FROM {hist}").fetchone()[0] == 2
    # dropping the form drops its history
    backend.drop_form(form, admin)
    assert con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE schema_name = 'crm' AND table_name = '_h__accounts'"
    ).fetchone()[0] == 0


def test_scd2_history_table_is_reconciled_when_it_falls_behind_the_form(backend, admin):
    """The history table outlives the flag, so it must be kept in step whatever the order.

    Regression: turning SCD2 off, adding a column and turning it back on used to leave a
    history table one column short, and every later save failed with a raw binder error.
    """
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(
        FormDef("crm", "accounts", scd2_enabled=True, columns=[ColumnDef("code")]), admin
    )
    con = backend._conn  # noqa: SLF001 - white-box check of the history table
    hist_cols = lambda: {  # noqa: E731
        r[0]
        for r in con.execute(
            "SELECT column_name FROM duckdb_columns() "
            "WHERE schema_name = 'crm' AND table_name = '_h__accounts'"
        ).fetchall()
    }

    form = backend.set_scd2(form, False, admin)
    form = backend.add_column(form, ColumnDef("label", DataType.STRING, description="d"), admin)
    assert "label" in hist_cols()  # maintained while the flag is off, because the table exists

    form = backend.set_scd2(form, True, admin)
    result = backend.apply_changes(form, ChangeSet(inserts=[RowInsert({"code": "A", "label": "Alpha"})]), admin)
    assert result.ok and result.inserted == 1
    assert con.execute('SELECT label FROM crm."_h__accounts" WHERE __END_AT IS NULL').fetchall() == [
        ("Alpha",)
    ]


def test_scd2_enabled_after_a_column_was_added_records_every_column(backend, admin):
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(FormDef("crm", "leads", columns=[ColumnDef("code")]), admin)
    form = backend.add_column(form, ColumnDef("note", DataType.STRING, description="d"), admin)
    form = backend.set_scd2(form, True, admin)
    result = backend.apply_changes(form, ChangeSet(inserts=[RowInsert({"code": "B", "note": "n"})]), admin)
    assert result.ok
    con = backend._conn  # noqa: SLF001
    assert con.execute('SELECT code, note FROM crm."_h__leads" WHERE __END_AT IS NULL').fetchall() == [
        ("B", "n")
    ]


def test_disabling_scd2_closes_the_open_windows(backend, admin):
    """``__END_AT IS NULL`` means "current version" to consumers, so it must not go stale.

    Regression: disabling SCD2 left every window open, a later edit made the history disagree
    with the form, and re-enabling skipped exactly the rows whose window was still open - so
    the history table advertised a superseded value as current, permanently.
    """
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(
        FormDef("crm", "accounts", scd2_enabled=True, columns=[ColumnDef("code"), ColumnDef("name")]),
        admin,
        rows=pd.DataFrame({"code": ["A"], "name": ["Alpha"]}),
    )
    con = backend._conn  # noqa: SLF001 - white-box check of the history table
    hist = 'crm."_h__accounts"'
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 1

    form = backend.set_scd2(form, False, admin)
    assert con.execute(f"SELECT count(*) FROM {hist} WHERE __END_AT IS NULL").fetchone()[0] == 0

    row = backend.read_rows(form).iloc[0]
    backend.apply_changes(
        form,
        ChangeSet(updates=[RowUpdate(str(row[ID_COLUMN]), {"name": "Alpha 2"}, int(row[VERSION_COLUMN]))]),
        admin,
    )
    form = backend.set_scd2(form, True, admin)
    assert con.execute(f"SELECT name FROM {hist} WHERE __END_AT IS NULL").fetchall() == [("Alpha 2",)]


def test_the_audit_trail_says_what_kind_of_object_changed(backend, admin):
    backend.create_function(FunctionDef("crm"), admin)
    form = backend.create_form(
        FormDef("crm", "accounts", columns=[ColumnDef("code")]), admin, rows=pd.DataFrame({"code": ["A"]})
    )
    backend.put_file(FileDef("crm", "leads.csv"), b"a,b\n1,2\n", admin)
    kinds = dict(
        backend._conn.execute(  # noqa: SLF001
            "SELECT table_name, object_type FROM _catalog.change_log GROUP BY table_name, object_type"
        ).fetchall()
    )
    assert kinds == {form.name: "form", "leads.csv": "file"}


def test_a_role_name_this_build_does_not_know_grants_nothing(backend, admin):
    """Roles are persisted by name: an unknown one must fail closed, not break the catalogue."""
    _functions(backend, admin, "crm")
    backend._conn.execute(  # noqa: SLF001
        "INSERT INTO _catalog.grants (schema_name, principal, role) VALUES ('crm', 'newcomers', 'APPROVER')"
    )
    permissions = backend.get_permissions(User("bob", groups=("newcomers",)))
    assert permissions.role_for("crm") is Role.NONE
    assert backend.list_function_grants("crm") == [("newcomers", Role.NONE)]
