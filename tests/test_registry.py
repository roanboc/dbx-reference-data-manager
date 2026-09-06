"""Conformance of the ``_catalog`` registry: one declaration, two dialects that stay in step.

These tests exist so that adding a registry field stays a one-line change. They fail loudly if
a column is declared for one backend and not the other, if an existing catalog is not upgraded
to a newly declared column, or if a writer goes back to positional ``INSERT ... VALUES``.
"""

from __future__ import annotations

import re

import duckdb
import pytest
from tests.test_databricks_backend import make_backend, statements

from rdm.backend import registry
from rdm.backend.duckdb_backend import META_SCHEMA, DuckDBBackend

# -- the declaration itself -------------------------------------------------------------------


def test_every_registry_table_declares_a_key_or_says_why_not():
    for table in registry.REGISTRY_TABLES:
        if table is registry.CHANGE_LOG:
            assert table.keys == ()  # append-only, identified per dialect
            continue
        assert table.keys, f"{table.name} has no key"
        for key in table.keys:
            assert key in table.column_names


def test_stamp_columns_close_every_described_table():
    stamps = tuple(name for name, _ in registry.STAMP_COLUMNS)
    for table in (registry.DOMAINS, registry.FUNCTIONS, registry.FORMS, registry.FILES):
        assert table.column_names[-len(stamps) :] == stamps


def test_only_keys_and_declared_required_columns_are_not_nullable():
    """A new column can never be NOT NULL: rows written before it existed have no value."""
    for table in registry.REGISTRY_TABLES:
        ddl = table.duckdb_ddl("t")
        for name, _kind in table.columns:
            not_null = re.search(rf"\b{name} \w+ NOT NULL", ddl) is not None
            assert not_null == (name in table.keys or name in table.required), name


# -- the two dialects agree -------------------------------------------------------------------


def _duckdb_columns(backend: DuckDBBackend, table: str) -> list[str]:
    return [
        c
        for (c,) in backend._conn.execute(  # noqa: SLF001 - the point of the test
            "SELECT column_name FROM duckdb_columns() WHERE schema_name = ? AND table_name = ? "
            "ORDER BY column_index",
            [META_SCHEMA, table],
        ).fetchall()
    ]


def _databricks_columns(ddl: str) -> list[str]:
    body = ddl[ddl.index("(") + 1 : ddl.rindex(") USING DELTA")]
    return [part.strip().split()[0].strip("`") for part in body.split(",")]


@pytest.mark.parametrize("table", registry.REGISTRY_TABLES, ids=lambda t: t.name)
def test_both_backends_create_exactly_what_the_registry_declares(table):
    """The conformance check: a column added for one backend is added for the other."""
    backend = DuckDBBackend(":memory:")
    try:
        local = _duckdb_columns(backend, table.name)
    finally:
        backend.close()
    production = _databricks_columns(table.databricks_ddl("`c`.`_catalog`.`x`"))

    # The identity column is declared per dialect and read by nobody else; everything after it
    # is the shared contract and must match exactly, in order.
    assert local[len(local) - len(table.columns) :] == list(table.column_names)
    assert production[len(production) - len(table.columns) :] == list(table.column_names)
    if table.duckdb_identity or table.databricks_identity:
        assert local[0] == production[0] == "id"
        assert len(local) == len(production) == len(table.columns) + 1
    else:
        assert local == production


# -- an existing catalog is upgraded ------------------------------------------------------


def test_an_existing_local_database_gains_a_newly_declared_column(tmp_path, monkeypatch):
    """Open an old database with a newer declaration: the column is added, data is kept."""
    path = tmp_path / "old.duckdb"
    backend = DuckDBBackend(str(path))
    backend._conn.execute(  # noqa: SLF001
        "INSERT INTO _catalog.domains (name, display_name) VALUES ('finance', 'Finance')"
    )
    backend.close()

    grown = registry.RegistryTable(
        name="domains",
        keys=registry.DOMAINS.keys,
        columns=(*registry.DOMAINS.columns, ("steward", registry.TEXT)),
        comment=registry.DOMAINS.comment,
    )
    monkeypatch.setattr(registry, "DOMAINS", grown)
    monkeypatch.setattr(registry, "REGISTRY_TABLES", (grown, *registry.REGISTRY_TABLES[1:]))

    backend = DuckDBBackend(str(path))
    try:
        assert "steward" in _duckdb_columns(backend, "domains")
        assert backend._conn.execute(  # noqa: SLF001
            "SELECT display_name, steward FROM _catalog.domains WHERE name = 'finance'"
        ).fetchone() == ("Finance", None)
    finally:
        backend.close()


def test_reopening_an_up_to_date_database_alters_nothing():
    backend = DuckDBBackend(":memory:")
    try:
        before = {t.name: _duckdb_columns(backend, t.name) for t in registry.REGISTRY_TABLES}
        backend._ensure_meta()  # noqa: SLF001 - idempotent by contract
        backend._ensure_meta()  # noqa: SLF001
        after = {t.name: _duckdb_columns(backend, t.name) for t in registry.REGISTRY_TABLES}
        assert before == after
    finally:
        backend.close()


def test_production_reconcile_adds_every_missing_column_not_just_the_one_it_knew_about():
    """The old self-heal matched the literal string 'owner_email'; this one is declarative."""
    backend, conn = make_backend(
        [
            (r"^MERGE INTO", None, _fail_once()),
            # information_schema reports a registry that predates two columns.
            (
                r"SELECT column_name FROM .*information_schema",
                ["column_name"],
                [(c,) for c in registry.FUNCTIONS.column_names if c not in {"owner_email", "doc_link"}],
            ),
        ]
    )
    backend._ensure_registry("functions")  # noqa: SLF001
    [(alter, _)] = statements(conn, r"^ALTER TABLE .*`functions` ADD COLUMNS")
    assert "`owner_email` STRING" in alter and "`doc_link` STRING" in alter


def _fail_once():
    state = {"first": True}

    def responder(_sql, _params):
        if state["first"]:
            state["first"] = False
            raise RuntimeError("UNRESOLVED_COLUMN: owner_email")
        return []

    return responder


# -- writers stay shape-tolerant ---------------------------------------------------------


def test_no_writer_uses_a_positional_insert_into_the_registry():
    """A positional INSERT breaks the moment a column is added; every writer must name them."""
    from pathlib import Path

    source = Path("src/rdm/backend/duckdb_backend.py").read_text()
    offenders = [
        line.strip()
        for line in source.splitlines()
        if re.search(r"INSERT (?:OR REPLACE )?INTO .*META_SCHEMA.*\}\s*(?:\"|')?\s*VALUES", line)
    ]
    assert offenders == []


def test_registry_reads_tolerate_a_column_this_build_has_never_heard_of():
    backend = DuckDBBackend(":memory:")
    try:
        backend._conn.execute("ALTER TABLE _catalog.domains ADD COLUMN invented_later VARCHAR")  # noqa: SLF001
        backend._conn.execute(  # noqa: SLF001
            "INSERT INTO _catalog.domains (name, display_name, invented_later) VALUES ('f', 'F', 'x')"
        )
        assert [d.name for d in backend.list_domains()] == ["f"]
    finally:
        backend.close()


def test_a_legacy_change_log_keeps_its_order_after_gaining_seq(tmp_path):
    """Entries written before ``seq`` existed are backfilled from the id they were given."""
    path = tmp_path / "legacy.duckdb"
    conn = duckdb.connect(str(path))
    conn.execute('CREATE SCHEMA "_catalog"')
    conn.execute(
        'CREATE TABLE "_catalog"."change_log" (id BIGINT PRIMARY KEY, schema_name VARCHAR NOT NULL, '
        "table_name VARCHAR NOT NULL, row_id VARCHAR, change_type VARCHAR NOT NULL, "
        "changed_at TIMESTAMP NOT NULL, changed_by VARCHAR, batch_id VARCHAR, before_json VARCHAR, "
        "after_json VARCHAR)"
    )
    for i in (1, 2, 3):
        conn.execute(
            "INSERT INTO \"_catalog\".\"change_log\" VALUES (?, 'fin', 'codes', ?, 'insert', now(), "
            "'a', 'b', NULL, NULL)",
            [i, f"row-{i}"],
        )
    conn.close()

    backend = DuckDBBackend(str(path))
    try:
        assert backend._conn.execute(  # noqa: SLF001
            "SELECT id, seq FROM _catalog.change_log ORDER BY seq"
        ).fetchall() == [(1, 1), (2, 2), (3, 3)]
    finally:
        backend.close()


# -- the one-time bootstrap an administrator runs ----------------------------------------


def test_bootstrap_script_emits_the_declared_shape_and_nothing_else():
    """The bootstrap DDL must come from the declaration, so it cannot drift from the app."""
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("bootstrap", Path("scripts/bootstrap_catalog.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    ddl = module.statements("_reference_data")
    assert ddl[0].startswith("CREATE SCHEMA IF NOT EXISTS `_reference_data`.`_catalog`")
    assert len(ddl) == 1 + len(registry.REGISTRY_TABLES)
    for table, statement in zip(registry.REGISTRY_TABLES, ddl[1:], strict=True):
        assert statement == table.databricks_ddl(f"`_reference_data`.`_catalog`.`{table.name}`")
    assert all("IF NOT EXISTS" in s for s in ddl)  # safe to re-run after every release


def test_bootstrap_script_refuses_a_catalog_name_that_is_not_an_identifier():
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location("bootstrap", Path("scripts/bootstrap_catalog.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with pytest.raises(ValueError, match="Invalid catalog name"):
        module.statements("bad name; DROP TABLE x")
