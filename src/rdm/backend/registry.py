"""The shape of the app's ``_catalog`` registry, declared once for both backends.

The registry is the app's own metadata store: the domain list, the descriptive attributes of
functions, forms and files, and the row-level audit trail. It exists in two dialects - DuckDB
locally, Delta in Unity Catalog in production - and both are generated from the tables
declared here.

**Adding a field is one entry in this module.** Both DDLs, the local upgrade of an existing
database and the production reconcile all read the same declaration, and
``tests/test_registry.py`` asserts that the two dialects stay in step. That replaces the
previous ritual (edit two DDLs, add a conditional ``ALTER`` to the DuckDB migration, and rely
on a Databricks self-heal that matched one hard-coded column name in a driver error message),
which was per-column, easy to get half-right, and silently lost writes on any catalog that
predated the change.

The evolution contract is the same as for the JSON metadata documents in :mod:`rdm.models`:

* **Additive only.** Add columns; never rename, retype or drop one. Every column is nullable,
  so an older build writing a row a newer build reads simply leaves the new field empty.
* **Reads are shape-tolerant.** Registry reads use ``SELECT *`` and zip the driver's column
  names, so a catalog carrying columns this build has never heard of is read without error.
* **Writes name their columns.** No positional ``INSERT``/``VALUES``, so an extra column in the
  table cannot break a writer.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Portable types the registry uses. Deliberately tiny: the registry describes objects, it is
#: not a place for user data, so it needs no decimals, dates or booleans.
TEXT = "TEXT"
NUMBER = "NUMBER"
MOMENT = "MOMENT"

_DUCKDB_TYPES = {TEXT: "VARCHAR", NUMBER: "BIGINT", MOMENT: "TIMESTAMP"}
#: Databricks stores the app's timestamps as ``TIMESTAMP_NTZ``: the app's canonical
#: representation is naive UTC, and NTZ is the type that round-trips it whatever the
#: warehouse's session zone is.
_DATABRICKS_TYPES = {TEXT: "STRING", NUMBER: "BIGINT", MOMENT: "TIMESTAMP_NTZ"}

#: Every registry entry records who created and last changed it, in this order, at the end.
STAMP_COLUMNS: tuple[tuple[str, str], ...] = (
    ("created_at", MOMENT),
    ("created_by", TEXT),
    ("updated_at", MOMENT),
    ("updated_by", TEXT),
)


@dataclass(frozen=True)
class RegistryTable:
    """One ``_catalog`` table: its key, its columns in DDL order and what it is for.

    ``columns`` is the *shared* contract - the columns both dialects must have, which is what
    the conformance test compares. ``duckdb_identity`` / ``databricks_identity`` are the one
    escape hatch: a leading identity column an engine declares for itself because the engines
    genuinely differ (DuckDB has sequences, Delta does not). Nothing outside a backend's own
    writer may read such a column, so it stays out of the shared contract.
    """

    name: str
    keys: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    comment: str
    #: Columns that must always carry a value even though they are not the key. Keep this list
    #: short: a new column is nullable by definition, because rows written before it existed
    #: cannot have one.
    required: tuple[str, ...] = ()
    #: Delta table properties, for tables production wants a change feed on.
    properties: tuple[tuple[str, str], ...] = ()
    duckdb_identity: str = ""
    databricks_identity: str = ""

    def _not_null(self, column: str) -> str:
        return " NOT NULL" if column in self.keys or column in self.required else ""

    @property
    def column_names(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.columns)

    def type_of(self, column: str, dialect: str) -> str:
        types = _DUCKDB_TYPES if dialect == "duckdb" else _DATABRICKS_TYPES
        return types[dict(self.columns)[column]]

    def duckdb_ddl(self, qualified_name: str) -> str:
        cols = [f"{name} {_DUCKDB_TYPES[kind]}{self._not_null(name)}" for name, kind in self.columns]
        if self.duckdb_identity:
            cols.insert(0, self.duckdb_identity)
        key = f",\n  PRIMARY KEY ({', '.join(self.keys)})" if self.keys else ""
        body = ",\n  ".join(cols)
        return f"CREATE TABLE IF NOT EXISTS {qualified_name} (\n  {body}{key})"

    def databricks_ddl(self, qualified_name: str) -> str:
        cols = [f"{name} {_DATABRICKS_TYPES[kind]}{self._not_null(name)}" for name, kind in self.columns]
        if self.databricks_identity:
            cols.insert(0, self.databricks_identity)
        props = ", ".join(f"'{k}' = '{v}'" for k, v in self.properties)
        return (
            f"CREATE TABLE IF NOT EXISTS {qualified_name} ({', '.join(cols)}) "
            f"USING DELTA COMMENT '{self.comment}'" + (f" TBLPROPERTIES ({props})" if props else "")
        )


def _described(*columns: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    """Key columns, then the attributes every described object carries, then the stamps."""
    return (*columns, *STAMP_COLUMNS)


#: The domain list. Domains are a classifier, not a Unity Catalog securable, so unlike every
#: other registry table this one is the *only* copy of its data - which is why its writes are
#: strict rather than best effort.
DOMAINS = RegistryTable(
    name="domains",
    keys=("name",),
    columns=_described(
        ("name", TEXT),
        ("display_name", TEXT),
        ("description", TEXT),
        ("owner", TEXT),
    ),
    comment="Domains (business classifier above functions) maintained by the Reference Data Manager",
)

FUNCTIONS = RegistryTable(
    name="functions",
    keys=("name",),
    columns=_described(
        ("name", TEXT),
        ("domain_name", TEXT),
        ("display_name", TEXT),
        ("description", TEXT),
        ("owner", TEXT),
        ("owner_email", TEXT),
        ("doc_link", TEXT),
    ),
    comment="Registry of functions (schemas) maintained by the Reference Data Manager",
)

FORMS = RegistryTable(
    name="forms",
    keys=("function_name", "name"),
    columns=_described(
        ("function_name", TEXT),
        ("name", TEXT),
        ("display_name", TEXT),
        ("description", TEXT),
        ("owner", TEXT),
        ("owner_email", TEXT),
    ),
    comment="Registry of forms maintained by the Reference Data Manager",
)

FILES = RegistryTable(
    name="files",
    keys=("function_name", "name"),
    columns=_described(
        ("function_name", TEXT),
        ("name", TEXT),
        ("display_name", TEXT),
        ("description", TEXT),
        ("owner", TEXT),
        ("owner_email", TEXT),
        ("size_bytes", NUMBER),
        ("row_count", NUMBER),
    ),
    comment=(
        "Registry of files (CSV/Parquet in the function volumes) maintained by the Reference Data Manager"
    ),
)

#: The audit trail: one entry per changed row and save, and per file upload, replacement and
#: deletion. ``before_json``/``after_json`` are opaque snapshots, which is what lets the audit
#: survive any later change to the forms it describes.
#:
#: ``seq`` is the ordinal each backend sorts the history by. The two fill it with the
#: strongest thing their engine offers, which is also why ``id`` is declared per dialect:
#: DuckDB has a sequence, so ``seq`` is globally monotonic and ordering by it alone is exact;
#: Delta has none, so Databricks fills ``seq`` with the entry's position within its batch and
#: orders by ``changed_at DESC, seq DESC``. That is exact within a batch and relies on the
#: app's clock across batches - worth knowing if several app instances ever write concurrently
#: with skewed clocks. Either way both expose the same ``version`` in ``get_history``: the
#: entry's position in the history shown.
CHANGE_LOG = RegistryTable(
    name="change_log",
    keys=(),
    required=("schema_name", "table_name", "change_type", "changed_at"),
    properties=(("delta.enableChangeDataFeed", "true"),),
    duckdb_identity="id BIGINT PRIMARY KEY",
    databricks_identity="id STRING NOT NULL",
    columns=(
        ("seq", NUMBER),
        ("schema_name", TEXT),
        ("table_name", TEXT),
        ("row_id", TEXT),
        ("change_type", TEXT),
        ("changed_at", MOMENT),
        ("changed_by", TEXT),
        ("batch_id", TEXT),
        ("before_json", TEXT),
        ("after_json", TEXT),
    ),
    comment="Row-level change history written by the Reference Data Manager app",
)

#: Every table the app keeps in ``_catalog``, in creation order.
REGISTRY_TABLES: tuple[RegistryTable, ...] = (DOMAINS, FUNCTIONS, FORMS, FILES, CHANGE_LOG)
BY_NAME: dict[str, RegistryTable] = {t.name: t for t in REGISTRY_TABLES}


def missing_columns(table: RegistryTable, present: set[str]) -> list[tuple[str, str]]:
    """Columns of ``table`` that a catalog does not have yet, in DDL order.

    This is the whole upgrade mechanism: both backends create what is declared here, compare
    it with what the catalog actually holds and add the difference. It is idempotent, order
    independent and needs no version bookkeeping, because the contract is additive.
    """
    return [(name, kind) for name, kind in table.columns if name not in present]
