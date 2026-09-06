"""DuckDB implementation of :class:`DatabaseBackend` for local development and tests.

DuckDB gives us real DDL/DML, ``COMMENT ON``, ``information_schema`` and transactions.
What Unity Catalog has and DuckDB lacks is emulated in a private ``_catalog`` schema, which
also holds the registry tables the Databricks backend maintains in ``<catalog>._catalog``:

* ``domains``            - the domain list (business classifier above functions)
* ``functions`` / ``forms`` / ``files`` - registry of functions (schemas), forms and files
* files themselves live on disk under ``<database dir>/files/<function>/<name>`` (the local
  stand-in for the function's Unity Catalog volume) and are read with DuckDB's CSV/Parquet
  readers
* ``change_log``         - row-level history (audit trail)
* ``object_properties``  - table properties / tags / schema comments (local emulation)
* ``grants``             - schema-level roles for groups (local emulation of UC grants)
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from rdm.backend import registry
from rdm.backend.base import BackendError, ConflictError, DatabaseBackend, NotFoundError
from rdm.backend.sql_utils import escape_literal_duckdb as lit
from rdm.backend.sql_utils import (
    native_type_duckdb,
    normalise_frame,
    parse_native_type,
    qualified,
    quote_ident,
    to_db_scalar,
    validate_identifier,
)
from rdm.coercion import CoercionError, coerce_value
from rdm.models import (
    CREATED_AT_COLUMN,
    CREATED_BY_COLUMN,
    FILE_CHANGE_TYPES,
    FILE_FORMATS,
    HISTORY_COLUMNS,
    ID_COLUMN,
    PROP_COLUMN_CONFIG,
    PROP_DISPLAY_NAME,
    PROP_DOC_LINK,
    PROP_DOMAIN,
    PROP_FORM,
    PROP_OWNER,
    PROP_OWNER_EMAIL,
    PROP_SCD2,
    PROP_SETTINGS,
    SCD2_END_COLUMN,
    SCD2_START_COLUMN,
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
    Permissions,
    Role,
    SaveResult,
    User,
    new_row_id,
    scd2_table_name,
    split_file_name,
    system_columns,
    validate_file_name,
)

log = logging.getLogger(__name__)

META_SCHEMA = "_catalog"
FILES_DIRNAME = "files"
#: Groups that exist in the local sandbox (the persona groups plus demo grants).
DEFAULT_LOCAL_GROUPS = (
    "rdm_admins",
    "everyone",
    "customer_stewards",
    "customer_readers",
    "finance_admins",
    "finance_stewards",
    "finance_readers",
    "hr_stewards",
    "hr_readers",
)
HIDDEN_SCHEMAS = frozenset({"main", "information_schema", "pg_catalog", "temp", META_SCHEMA})
CATALOG_LEVEL = "*"


def utcnow() -> datetime:
    """Naive UTC timestamps are the app's canonical representation."""
    return datetime.now(UTC).replace(tzinfo=None)


def _coerce_for_import(col: ColumnDef, value: Any) -> Any:
    try:
        return coerce_value(col, value)
    except CoercionError as exc:
        raise BackendError(f"Import failed: column '{col.name}': {exc}") from exc


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal | date | datetime | pd.Timestamp):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class DuckDBBackend(DatabaseBackend):
    name = "duckdb"

    def __init__(self, path: str = ":memory:", files_dir: str | None = None) -> None:
        self.path = path
        self._temp_files_dir = False
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        if files_dir:
            self.files_dir = Path(files_dir)
        elif path != ":memory:":
            self.files_dir = Path(path).parent / FILES_DIRNAME
        else:
            self.files_dir = Path(tempfile.mkdtemp(prefix="rdm_files_"))
            self._temp_files_dir = True
        self._conn = duckdb.connect(path)
        self._ensure_meta()

    # -- infrastructure ----------------------------------------------------------------------

    def describe(self) -> str:
        return f"DuckDB ({self.path})"

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover
            pass
        if self._temp_files_dir:
            shutil.rmtree(self.files_dir, ignore_errors=True)

    @contextmanager
    def _cursor(self) -> Iterator[duckdb.DuckDBPyConnection]:
        cur = self._conn.cursor()
        try:
            yield cur
        finally:
            cur.close()

    @contextmanager
    def _tx(self) -> Iterator[duckdb.DuckDBPyConnection]:
        cur = self._conn.cursor()
        cur.begin()
        try:
            yield cur
            cur.commit()
        except Exception:
            cur.rollback()
            raise
        finally:
            cur.close()

    def _ensure_meta(self) -> None:
        """Create or upgrade the local ``_catalog`` schema, transactionally.

        Everything the app keeps about itself is declared in :mod:`rdm.backend.registry`; what
        is missing from an existing database is added from that declaration, so opening an
        older database upgrades it and opening a newer one leaves its extra columns alone.
        """
        with self._tx() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(META_SCHEMA)}")
            self._migrate_meta(cur)
            # Local emulation of what Unity Catalog provides natively; not part of the registry
            # contract, because production has no equivalent table to keep in step with.
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {qualified([META_SCHEMA, "object_properties"])} (
                        object_type VARCHAR NOT NULL,
                        schema_name VARCHAR NOT NULL,
                        table_name  VARCHAR NOT NULL,
                        key         VARCHAR NOT NULL,
                        value       VARCHAR,
                        PRIMARY KEY (object_type, schema_name, table_name, key))"""
            )
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {qualified([META_SCHEMA, "grants"])} (
                        schema_name VARCHAR NOT NULL,
                        principal   VARCHAR NOT NULL,
                        role        VARCHAR NOT NULL,
                        PRIMARY KEY (schema_name, principal))"""
            )
            cur.execute(f"CREATE SEQUENCE IF NOT EXISTS {qualified([META_SCHEMA, 'change_log_seq'])}")
            for table in registry.REGISTRY_TABLES:
                cur.execute(table.duckdb_ddl(qualified([META_SCHEMA, table.name])))
                self._reconcile(cur, table)

    @staticmethod
    def _reconcile(cur, table: registry.RegistryTable) -> None:
        """Add the columns an existing registry table is missing (never drop or retype).

        This is the whole upgrade path for a registry field: declare it in
        :mod:`rdm.backend.registry` and every database picks it up on the next open.
        """
        present = {
            c
            for (c,) in cur.execute(
                "SELECT column_name FROM duckdb_columns() WHERE schema_name = ? AND table_name = ?",
                [META_SCHEMA, table.name],
            ).fetchall()
        }
        for name, _kind in registry.missing_columns(table, present):
            cur.execute(
                f"ALTER TABLE {qualified([META_SCHEMA, table.name])} "
                f"ADD COLUMN {quote_ident(name)} {table.type_of(name, 'duckdb')}"
            )
        if table is registry.CHANGE_LOG and "seq" not in present and present:
            # Entries written before ``seq`` existed: the old sequence id is exactly the
            # ordinal it now carries, so history keeps its order across the upgrade.
            cur.execute(f"UPDATE {qualified([META_SCHEMA, table.name])} SET seq = CAST(id AS BIGINT)")

    @staticmethod
    def _migrate_meta(cur) -> None:
        """Rename what a *renamed* concept left behind; adding columns is not done here.

        The old ``domains`` registry (with a ``doc_link`` column) described what are now
        *functions*; the old ``forms`` registry keyed forms by ``domain``. Renames are the only
        thing that cannot be expressed additively, so they are the only thing in this function;
        every new registry column is picked up by :meth:`_reconcile` from
        :mod:`rdm.backend.registry` instead.
        """
        cols = {
            (t, c)
            for t, c in cur.execute(
                "SELECT table_name, column_name FROM duckdb_columns() WHERE schema_name = ?", [META_SCHEMA]
            ).fetchall()
        }
        if ("domains", "doc_link") in cols and not any(t == "functions" for t, _ in cols):
            cur.execute(
                f"ALTER TABLE {qualified([META_SCHEMA, 'domains'])} RENAME TO {quote_ident('functions')}"
            )
            cur.execute(f"ALTER TABLE {qualified([META_SCHEMA, 'functions'])} ADD COLUMN domain_name VARCHAR")
        if ("forms", "domain") in cols:
            cur.execute(
                f"ALTER TABLE {qualified([META_SCHEMA, 'forms'])} RENAME COLUMN domain TO function_name"
            )

    # -- registry (mirrors the Databricks ``_catalog`` tables) -----------------------------

    def _register_function(self, cur, function: FunctionDef, actor: User) -> None:
        now = utcnow()
        existing = cur.execute(
            f"SELECT created_at, created_by FROM {qualified([META_SCHEMA, 'functions'])} WHERE name = ?",
            [function.name],
        ).fetchone()
        created_at, created_by = existing if existing else (now, actor.username)
        cur.execute(
            f"INSERT OR REPLACE INTO {qualified([META_SCHEMA, 'functions'])} "
            "(name, domain_name, display_name, description, owner, owner_email, doc_link, created_at, "
            "created_by, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                function.name,
                function.domain or None,
                function.display_name,
                function.description,
                function.owner,
                function.owner_email,
                function.doc_link,
                created_at,
                created_by,
                now,
                actor.username,
            ],
        )

    def _unregister_function(self, cur, name: str) -> None:
        cur.execute(f"DELETE FROM {qualified([META_SCHEMA, 'functions'])} WHERE name = ?", [name])

    def _register_form(self, cur, form: FormDef, actor: User) -> None:
        now = utcnow()
        existing = cur.execute(
            f"SELECT created_at, created_by FROM {qualified([META_SCHEMA, 'forms'])} "
            "WHERE function_name = ? AND name = ?",
            [form.function, form.name],
        ).fetchone()
        created_at, created_by = existing if existing else (now, actor.username)
        cur.execute(
            f"INSERT OR REPLACE INTO {qualified([META_SCHEMA, 'forms'])} "
            "(function_name, name, display_name, description, owner, owner_email, created_at, created_by, "
            "updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                form.function,
                form.name,
                form.display_name,
                form.description,
                form.owner,
                form.owner_email,
                created_at,
                created_by,
                now,
                actor.username,
            ],
        )

    def _unregister_form(self, cur, function: str, name: str) -> None:
        cur.execute(
            f"DELETE FROM {qualified([META_SCHEMA, 'forms'])} WHERE function_name = ? AND name = ?",
            [function, name],
        )

    def _register_file(self, cur, file: FileDef, actor: User) -> None:
        now = utcnow()
        existing = cur.execute(
            f"SELECT created_at, created_by FROM {qualified([META_SCHEMA, 'files'])} "
            "WHERE function_name = ? AND name = ?",
            [file.function, file.name],
        ).fetchone()
        created_at, created_by = existing if existing else (now, actor.username)
        cur.execute(
            f"INSERT OR REPLACE INTO {qualified([META_SCHEMA, 'files'])} "
            "(function_name, name, display_name, description, owner, owner_email, size_bytes, row_count, "
            "created_at, created_by, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                file.function,
                file.name,
                file.display_name,
                file.description,
                file.owner,
                file.owner_email,
                file.size_bytes,
                file.row_count,
                created_at,
                created_by,
                now,
                actor.username,
            ],
        )

    def _unregister_file(self, cur, function: str, name: str) -> None:
        cur.execute(
            f"DELETE FROM {qualified([META_SCHEMA, 'files'])} WHERE function_name = ? AND name = ?",
            [function, name],
        )

    def _file_registry(self, cur, function: str) -> dict[str, dict[str, Any]]:
        cols = [
            "name",
            "display_name",
            "description",
            "owner",
            "owner_email",
            "size_bytes",
            "row_count",
            "created_at",
            "created_by",
            "updated_at",
            "updated_by",
        ]
        rows = cur.execute(
            f"SELECT {', '.join(cols)} FROM {qualified([META_SCHEMA, 'files'])} WHERE function_name = ?",
            [function],
        ).fetchall()
        return {r[0]: dict(zip(cols, r, strict=True)) for r in rows}

    @staticmethod
    def _t(form: FormDef) -> str:
        return qualified([form.function, form.name])

    # -- property store -------------------------------------------------------------------

    def _get_props(self, cur, object_type: str, schema: str, table: str = "") -> dict[str, str]:
        rows = cur.execute(
            f"SELECT key, value FROM {qualified([META_SCHEMA, 'object_properties'])} "
            "WHERE object_type = ? AND schema_name = ? AND table_name = ?",
            [object_type, schema, table],
        ).fetchall()
        return {k: v for k, v in rows if v is not None}

    def _get_props_bulk(self, cur, object_type: str, schema: str) -> dict[str, dict[str, str]]:
        rows = cur.execute(
            f"SELECT table_name, key, value FROM {qualified([META_SCHEMA, 'object_properties'])} "
            "WHERE object_type = ? AND schema_name = ?",
            [object_type, schema],
        ).fetchall()
        out: dict[str, dict[str, str]] = {}
        for table, k, v in rows:
            if v is not None:
                out.setdefault(table, {})[k] = v
        return out

    def _set_props(
        self, cur, object_type: str, schema: str, table: str, props: dict[str, str | None]
    ) -> None:
        for key, value in props.items():
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'object_properties'])} "
                "WHERE object_type = ? AND schema_name = ? AND table_name = ? AND key = ?",
                [object_type, schema, table, key],
            )
            if value is not None and value != "":
                cur.execute(
                    f"INSERT INTO {qualified([META_SCHEMA, 'object_properties'])} "
                    "(object_type, schema_name, table_name, key, value) VALUES (?, ?, ?, ?, ?)",
                    [object_type, schema, table, key, str(value)],
                )

    def _delete_props(self, cur, schema: str, table: str | None = None) -> None:
        if table is None:
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'object_properties'])} WHERE schema_name = ?", [schema]
            )
        else:
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'object_properties'])} WHERE schema_name = ? AND table_name = ?",
                [schema, table],
            )

    # -- domains (classifier) ---------------------------------------------------------------

    def _function_counts_by_domain(self, cur) -> dict[str, int]:
        rows = cur.execute(
            f"SELECT value, count(*) FROM {qualified([META_SCHEMA, 'object_properties'])} "
            "WHERE object_type = 'schema' AND key = ? GROUP BY value",
            [PROP_DOMAIN],
        ).fetchall()
        return {str(d): int(n) for d, n in rows if d}

    @staticmethod
    def _domain_from_row(row: tuple, count: int | None) -> DomainDef:
        name, display_name, description, owner = row
        return DomainDef(
            name=name,
            display_name=display_name or "",
            description=description or "",
            owner=owner or "",
            function_count=count,
        )

    def list_domains(self) -> list[DomainDef]:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT name, display_name, description, owner FROM {qualified([META_SCHEMA, 'domains'])} "
                "ORDER BY name"
            ).fetchall()
            counts = self._function_counts_by_domain(cur)
        return [self._domain_from_row(r, counts.get(r[0], 0)) for r in rows]

    def get_domain(self, name: str) -> DomainDef:
        with self._cursor() as cur:
            row = cur.execute(
                f"SELECT name, display_name, description, owner FROM {qualified([META_SCHEMA, 'domains'])} "
                "WHERE name = ?",
                [name],
            ).fetchone()
            if row is None:
                raise NotFoundError(f"Domain '{name}' does not exist.")
            counts = self._function_counts_by_domain(cur)
        return self._domain_from_row(row, counts.get(name, 0))

    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        now = utcnow()
        with self._tx() as cur:
            exists = cur.execute(
                f"SELECT 1 FROM {qualified([META_SCHEMA, 'domains'])} WHERE name = ?", [domain.name]
            ).fetchone()
            if exists:
                raise ConflictError(f"Domain '{domain.name}' already exists.")
            cur.execute(
                f"INSERT INTO {qualified([META_SCHEMA, 'domains'])} "
                "(name, display_name, description, owner, created_at, created_by, updated_at, updated_by) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    domain.name,
                    domain.display_name,
                    domain.description,
                    domain.owner or actor.username,
                    now,
                    actor.username,
                    now,
                    actor.username,
                ],
            )
        return self.get_domain(domain.name)

    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        with self._tx() as cur:
            n = cur.execute(
                f"UPDATE {qualified([META_SCHEMA, 'domains'])} SET display_name = ?, description = ?, "
                "owner = ?, updated_at = ?, updated_by = ? WHERE name = ?",
                [
                    domain.display_name,
                    domain.description,
                    domain.owner,
                    utcnow(),
                    actor.username,
                    domain.name,
                ],
            ).fetchone()[0]
            if n == 0:
                raise NotFoundError(f"Domain '{domain.name}' does not exist.")
        return self.get_domain(domain.name)

    def delete_domain(self, name: str, actor: User) -> None:
        with self._tx() as cur:
            exists = cur.execute(
                f"SELECT 1 FROM {qualified([META_SCHEMA, 'domains'])} WHERE name = ?", [name]
            ).fetchone()
            if not exists:
                raise NotFoundError(f"Domain '{name}' does not exist.")
            assigned = self._function_counts_by_domain(cur).get(name, 0)
            if assigned:
                raise ConflictError(
                    f"Domain '{name}' still has {assigned} function(s) assigned. Move them to another domain first."
                )
            cur.execute(f"DELETE FROM {qualified([META_SCHEMA, 'domains'])} WHERE name = ?", [name])

    # -- functions (schemas) ------------------------------------------------------------------

    def _schema_exists(self, cur, name: str) -> bool:
        row = cur.execute(
            "SELECT 1 FROM duckdb_schemas() WHERE NOT internal AND schema_name = ?", [name]
        ).fetchone()
        return row is not None

    def list_functions(self) -> list[FunctionDef]:
        with self._cursor() as cur:
            names = [
                r[0]
                for r in cur.execute(
                    "SELECT schema_name FROM duckdb_schemas() WHERE NOT internal ORDER BY schema_name"
                ).fetchall()
                if r[0] not in HIDDEN_SCHEMAS
            ]
            counts = dict(
                cur.execute(
                    "SELECT schema_name, count(*) FROM duckdb_tables() WHERE NOT internal AND NOT temporary "
                    "AND table_name NOT LIKE '\\_%' ESCAPE '\\' "
                    "GROUP BY schema_name"
                ).fetchall()
            )
            return [self._function_from(cur, n, counts.get(n, 0)) for n in names]

    def _stored_file_names(self, function: str) -> list[str]:
        folder = self.files_dir / function
        if not folder.is_dir():
            return []
        return sorted(
            p.name for p in folder.iterdir() if p.is_file() and split_file_name(p.name)[1] in FILE_FORMATS
        )

    def _function_from(self, cur, name: str, form_count: int | None) -> FunctionDef:
        props = self._get_props(cur, "schema", name)
        return FunctionDef(
            name=name,
            display_name=props.get(PROP_DISPLAY_NAME, ""),
            description=props.get("comment", ""),
            owner=props.get(PROP_OWNER, ""),
            owner_email=props.get(PROP_OWNER_EMAIL, ""),
            doc_link=props.get(PROP_DOC_LINK, ""),
            domain=props.get(PROP_DOMAIN, ""),
            form_count=form_count,
            file_count=len(self._stored_file_names(name)),
            properties=props,
        )

    def get_function(self, name: str) -> FunctionDef:
        with self._cursor() as cur:
            if name in HIDDEN_SCHEMAS or not self._schema_exists(cur, name):
                raise NotFoundError(f"Function '{name}' does not exist.")
            count = cur.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND NOT temporary AND schema_name = ? "
                "AND table_name NOT LIKE '\\_%' ESCAPE '\\'",
                [name],
            ).fetchone()[0]
            return self._function_from(cur, name, count)

    def _check_domain(self, cur, domain: str) -> None:
        if not domain:
            return
        row = cur.execute(
            f"SELECT 1 FROM {qualified([META_SCHEMA, 'domains'])} WHERE name = ?", [domain]
        ).fetchone()
        if row is None:
            raise NotFoundError(f"Domain '{domain}' does not exist.")

    def create_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        function.validate()
        if function.name in HIDDEN_SCHEMAS:
            raise ConflictError(f"'{function.name}' is a reserved name.")
        with self._tx() as cur:
            if self._schema_exists(cur, function.name):
                raise ConflictError(f"Function '{function.name}' already exists.")
            self._check_domain(cur, function.domain)
            cur.execute(f"CREATE SCHEMA {quote_ident(function.name)}")
            self._set_props(
                cur,
                "schema",
                function.name,
                "",
                {
                    "comment": function.description,
                    PROP_DISPLAY_NAME: function.display_name,
                    PROP_OWNER: function.owner or actor.username,
                    PROP_OWNER_EMAIL: function.owner_email,
                    PROP_DOC_LINK: function.doc_link,
                    PROP_DOMAIN: function.domain,
                    "created_by": actor.username,
                },
            )
            self._register_function(cur, function, actor)
        return self.get_function(function.name)

    def update_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        function.validate()
        with self._tx() as cur:
            if not self._schema_exists(cur, function.name):
                raise NotFoundError(f"Function '{function.name}' does not exist.")
            self._check_domain(cur, function.domain)
            self._set_props(
                cur,
                "schema",
                function.name,
                "",
                {
                    "comment": function.description,
                    PROP_DISPLAY_NAME: function.display_name,
                    PROP_OWNER: function.owner,
                    PROP_OWNER_EMAIL: function.owner_email,
                    PROP_DOC_LINK: function.doc_link,
                    PROP_DOMAIN: function.domain,
                },
            )
            self._register_function(cur, function, actor)
        return self.get_function(function.name)

    def drop_function(self, function: FunctionDef, actor: User) -> None:
        with self._tx() as cur:
            if not self._schema_exists(cur, function.name):
                raise NotFoundError(f"Function '{function.name}' does not exist.")
            n = cur.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND NOT temporary AND schema_name = ? "
                "AND table_name NOT LIKE '\\_%' ESCAPE '\\'",
                [function.name],
            ).fetchone()[0]
            if n:
                raise ConflictError(
                    f"Function '{function.name}' still has {n} form(s). Delete or migrate them first."
                )
            folder = self.files_dir / function.name
            # Anything in the folder counts, not only CSV/Parquet: it is removed with the function.
            n_entries = len(list(folder.iterdir())) if folder.is_dir() else 0
            if n_entries:
                raise ConflictError(
                    f"Function '{function.name}' still has {n_entries} file(s) or folder(s). "
                    "Delete or migrate them first."
                )
            cur.execute(f"DROP SCHEMA {quote_ident(function.name)} RESTRICT")
            self._delete_props(cur, function.name)
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'grants'])} WHERE schema_name = ?", [function.name]
            )
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'files'])} WHERE function_name = ?", [function.name]
            )
            self._unregister_function(cur, function.name)
            if folder.is_dir():
                folder.rmdir()  # verified empty above; never a recursive delete

    # -- forms -------------------------------------------------------------------------------

    def list_forms(self, function: str) -> list[FormDef]:
        with self._cursor() as cur:
            if not self._schema_exists(cur, function):
                raise NotFoundError(f"Function '{function}' does not exist.")
            rows = cur.execute(
                "SELECT table_name, comment, estimated_size FROM duckdb_tables() "
                "WHERE NOT internal AND NOT temporary AND schema_name = ? "
                "AND table_name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY table_name",
                [function],
            ).fetchall()
            props = self._get_props_bulk(cur, "table", function)
            tags = self._get_props_bulk(cur, "table_tag", function)
            forms = []
            for name, comment, size in rows:
                p = props.get(name, {})
                forms.append(
                    FormDef(
                        function=function,
                        name=name,
                        display_name=p.get(PROP_DISPLAY_NAME, ""),
                        description=comment or "",
                        owner=p.get(PROP_OWNER, ""),
                        owner_email=p.get(PROP_OWNER_EMAIL, ""),
                        scd2_enabled=p.get(PROP_SCD2, "") == "true",
                        properties=p,
                        tags=tags.get(name, {}),
                        row_count=int(size) if size is not None else None,
                    )
                )
            return forms

    def _table_exists(self, cur, function: str, name: str) -> bool:
        row = cur.execute(
            "SELECT 1 FROM duckdb_tables() WHERE NOT internal AND NOT temporary AND schema_name = ? AND table_name = ?",
            [function, name],
        ).fetchone()
        return row is not None

    def _load_columns(self, cur, function: str, name: str) -> list[ColumnDef]:
        rows = cur.execute(
            "SELECT column_name, data_type, is_nullable, comment, column_index FROM duckdb_columns() "
            "WHERE NOT internal AND schema_name = ? AND table_name = ? ORDER BY column_index",
            [function, name],
        ).fetchall()
        cols = []
        for i, (cname, dtype, nullable, comment, _idx) in enumerate(rows):
            t, p, s = parse_native_type(dtype)
            cols.append(
                ColumnDef(
                    name=cname,
                    data_type=t,
                    description=comment or "",
                    nullable=bool(nullable),
                    precision=p,
                    scale=s,
                    native_type=dtype,
                    position=i,
                )
            )
        return cols

    def get_form(self, function: str, name: str) -> FormDef:
        with self._cursor() as cur:
            if not self._table_exists(cur, function, name):
                raise NotFoundError(f"Form '{function}.{name}' does not exist.")
            comment = cur.execute(
                "SELECT comment FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?",
                [function, name],
            ).fetchone()[0]
            props = self._get_props(cur, "table", function, name)
            tags = self._get_props(cur, "table_tag", function, name)
            form = FormDef(
                function=function,
                name=name,
                display_name=props.get(PROP_DISPLAY_NAME, ""),
                description=comment or "",
                owner=props.get(PROP_OWNER, ""),
                owner_email=props.get(PROP_OWNER_EMAIL, ""),
                scd2_enabled=props.get(PROP_SCD2, "") == "true",
                columns=self._load_columns(cur, function, name),
                properties=props,
                tags=tags,
            )
            form.apply_column_config(props.get(PROP_COLUMN_CONFIG))
            form.apply_settings(props.get(PROP_SETTINGS))
            form.row_count = cur.execute(f"SELECT count(*) FROM {self._t(form)}").fetchone()[0]
            if form.has_system_columns:
                row = cur.execute(
                    f"SELECT min({quote_ident(CREATED_AT_COLUMN)}), max({quote_ident(UPDATED_AT_COLUMN)}) FROM {self._t(form)}"
                ).fetchone()
                form.created_at, form.updated_at = row[0], row[1]
                last = cur.execute(
                    f"SELECT changed_at, changed_by FROM {qualified([META_SCHEMA, 'change_log'])} "
                    "WHERE schema_name = ? AND table_name = ? ORDER BY id DESC LIMIT 1",
                    [function, name],
                ).fetchone()
                if last is not None:
                    form.updated_at, form.updated_by = last[0], last[1] or ""
            if "created_at" in props:
                try:
                    form.created_at = datetime.fromisoformat(props["created_at"])
                except ValueError:
                    pass
            return form

    @staticmethod
    def _column_ddl(col: ColumnDef) -> str:
        ddl = f"{quote_ident(col.name)} {native_type_duckdb(col)}"
        if not col.nullable:
            ddl += " NOT NULL"
        return ddl

    def create_form(self, form: FormDef, actor: User, rows: pd.DataFrame | None = None) -> FormDef:
        # System columns first, then the user's columns (in the order given).
        user_cols = [c for c in form.columns if not c.is_system]
        form.columns = system_columns() + user_cols
        form.validate()
        with self._tx() as cur:
            if not self._schema_exists(cur, form.function):
                raise NotFoundError(f"Function '{form.function}' does not exist.")
            if self._table_exists(cur, form.function, form.name):
                raise ConflictError(f"Form '{form.full_name}' already exists.")
            col_ddl = ",\n  ".join(self._column_ddl(c) for c in form.columns)
            cur.execute(
                f"CREATE TABLE {self._t(form)} (\n  {col_ddl},\n  PRIMARY KEY ({quote_ident(ID_COLUMN)}))"
            )
            self._write_comments(cur, form)
            self._set_props(
                cur,
                "table",
                form.function,
                form.name,
                {
                    PROP_FORM: "true",
                    PROP_DISPLAY_NAME: form.display_name,
                    PROP_OWNER: form.owner or actor.username,
                    PROP_OWNER_EMAIL: form.owner_email,
                    PROP_SCD2: "true" if form.scd2_enabled else "",
                    PROP_COLUMN_CONFIG: form.column_config_json(),
                    PROP_SETTINGS: form.settings_json() if form.settings else "",
                    "created_by": actor.username,
                    "created_at": utcnow().isoformat(),
                },
            )
            self._set_props(
                cur,
                "table_tag",
                form.function,
                form.name,
                {
                    "rdm_form": "true",
                    "rdm_display_name": form.display_name,
                    "rdm_owner": form.owner or actor.username,
                },
            )
            self._register_form(cur, form, actor)
            if form.scd2_enabled:
                scd2_table_name(form.name)  # length check before the table is created
                self._scd2_create(cur, form)
            if rows is not None and len(rows):
                self._append_rows(cur, form, rows, actor)
        return self.get_form(form.function, form.name)

    def _write_comments(self, cur, form: FormDef) -> None:
        cur.execute(f"COMMENT ON TABLE {self._t(form)} IS {lit(form.description or '')}")
        for c in form.columns:
            cur.execute(
                f"COMMENT ON COLUMN {self._t(form)}.{quote_ident(c.name)} IS {lit(c.description or '')}"
            )

    def update_form_metadata(self, form: FormDef, actor: User) -> FormDef:
        form.validate()
        with self._tx() as cur:
            if not self._table_exists(cur, form.function, form.name):
                raise NotFoundError(f"Form '{form.full_name}' does not exist.")
            existing = {c.name: c for c in self._load_columns(cur, form.function, form.name)}
            for c in form.columns:
                if c.name not in existing:
                    raise NotFoundError(f"Column '{c.name}' does not exist on '{form.full_name}'.")
            self._write_comments(cur, form)
            for c in form.user_columns:
                if existing[c.name].nullable != c.nullable:
                    clause = "DROP NOT NULL" if c.nullable else "SET NOT NULL"
                    try:
                        cur.execute(
                            f"ALTER TABLE {self._t(form)} ALTER COLUMN {quote_ident(c.name)} {clause}"
                        )
                    except duckdb.Error as exc:
                        raise BackendError(
                            f"Cannot make '{c.name}' required: {exc}. Fill in the empty values first."
                        ) from exc
            self._set_props(
                cur,
                "table",
                form.function,
                form.name,
                {
                    PROP_DISPLAY_NAME: form.display_name,
                    PROP_OWNER: form.owner,
                    PROP_OWNER_EMAIL: form.owner_email,
                    PROP_COLUMN_CONFIG: form.column_config_json(),
                    PROP_SETTINGS: form.settings_json() if form.settings else "",
                },
            )
            self._set_props(
                cur,
                "table_tag",
                form.function,
                form.name,
                {"rdm_display_name": form.display_name, "rdm_owner": form.owner},
            )
            self._register_form(cur, form, actor)
        return self.get_form(form.function, form.name)

    def add_column(self, form: FormDef, column: ColumnDef, actor: User) -> FormDef:
        column.validate()
        if column.is_system:
            raise BackendError("System columns cannot be added manually.")
        with self._tx() as cur:
            if form.column(column.name) is not None or column.name in {
                c.name for c in self._load_columns(cur, form.function, form.name)
            }:
                raise ConflictError(f"Column '{column.name}' already exists.")
            cur.execute(
                f"ALTER TABLE {self._t(form)} ADD COLUMN {quote_ident(column.name)} {native_type_duckdb(column)}"
            )
            cur.execute(
                f"COMMENT ON COLUMN {self._t(form)}.{quote_ident(column.name)} IS {lit(column.description or '')}"
            )
            # The history table is maintained whenever it exists, not only while the flag is
            # on: it outlives set_scd2(False), so keying this on the flag let it fall behind.
            if self._table_exists(cur, form.function, scd2_table_name(form.name)):
                cur.execute(
                    f"ALTER TABLE {self._h(form)} ADD COLUMN {quote_ident(column.name)} {native_type_duckdb(column)}"
                )
            column.nullable = True  # existing rows have no value; NOT NULL can be set later
            form.columns.append(column)
            self._set_props(
                cur, "table", form.function, form.name, {PROP_COLUMN_CONFIG: form.column_config_json()}
            )
        return self.get_form(form.function, form.name)

    def drop_column(self, form: FormDef, column_name: str, actor: User) -> FormDef:
        if column_name in SYSTEM_COLUMNS:
            raise BackendError("System columns cannot be removed.")
        with self._tx() as cur:
            names = {c.name for c in self._load_columns(cur, form.function, form.name)}
            if column_name not in names:
                raise NotFoundError(f"Column '{column_name}' does not exist.")
            cur.execute(f"ALTER TABLE {self._t(form)} DROP COLUMN {quote_ident(column_name)}")
            if self._table_exists(cur, form.function, scd2_table_name(form.name)):
                cur.execute(f"ALTER TABLE {self._h(form)} DROP COLUMN {quote_ident(column_name)}")
            form.columns = [c for c in form.columns if c.name != column_name]
            self._set_props(
                cur, "table", form.function, form.name, {PROP_COLUMN_CONFIG: form.column_config_json()}
            )
        return self.get_form(form.function, form.name)

    def drop_form(self, form: FormDef, actor: User) -> None:
        with self._tx() as cur:
            if not self._table_exists(cur, form.function, form.name):
                raise NotFoundError(f"Form '{form.full_name}' does not exist.")
            cur.execute(f"DROP TABLE {self._t(form)}")
            cur.execute(f"DROP TABLE IF EXISTS {self._h(form)}")
            self._delete_props(cur, form.function, form.name)
            self._unregister_form(cur, form.function, form.name)
            # The audit trail is deliberately *not* deleted: it is the governance record of who
            # changed what, it outlives the object it describes (FUNCTIONAL_DESIGN.md 7.5), and
            # the Databricks backend keeps it. A form recreated under the same name therefore
            # inherits those entries - names are the key, see docs/DATA_MODEL.md 3.

    # -- rows --------------------------------------------------------------------------------

    def _select_columns(self, form: FormDef) -> list[str]:
        return [c.name for c in form.columns] if form.columns else []

    def read_rows(
        self,
        form: FormDef,
        search: str | None = None,
        limit: int = 5000,
        order_by: str | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        cols = self._select_columns(form)
        select = ", ".join(quote_ident(c) for c in cols) if cols else "*"
        params: list[Any] = []
        where = ""
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            search_cols = [c.name for c in form.columns if not c.is_system] or cols
            clauses = [f"CAST({quote_ident(c)} AS VARCHAR) ILIKE ? ESCAPE '\\'" for c in search_cols]
            where = " WHERE " + " OR ".join(clauses)
            params.extend([pattern] * len(clauses))
        direction = "DESC" if descending else "ASC"
        if order_by and order_by in cols:
            order = f" ORDER BY {quote_ident(order_by)} {direction} NULLS LAST"
            if form.has_system_columns:
                order += f", {quote_ident(ID_COLUMN)}"
        elif form.key_columns:
            keys = ", ".join(f"{quote_ident(k.name)} {direction} NULLS LAST" for k in form.key_columns)
            order = f" ORDER BY {keys}, {quote_ident(ID_COLUMN)}"
        elif form.has_system_columns:
            order = (
                f" ORDER BY {quote_ident(CREATED_AT_COLUMN)} {direction} NULLS LAST, {quote_ident(ID_COLUMN)}"
            )
        elif cols:
            order = f" ORDER BY {quote_ident(cols[0])} {direction}"
        else:
            order = ""
        params.append(int(limit))
        with self._cursor() as cur:
            df = cur.execute(f"SELECT {select} FROM {self._t(form)}{where}{order} LIMIT ?", params).df()
        return normalise_frame(df, form)

    def count_rows(self, form: FormDef) -> int:
        with self._cursor() as cur:
            return int(cur.execute(f"SELECT count(*) FROM {self._t(form)}").fetchone()[0])

    def _log(
        self,
        cur,
        form: FormDef,
        row_id: str | None,
        change_type: str,
        actor: User,
        batch: str,
        before: dict | None,
        after: dict | None,
        when: datetime,
    ) -> None:
        # ``seq`` carries the same value as the local id: it is what both backends order the
        # history by, and Databricks fills it with a per-batch ordinal (Delta has no sequence).
        seq = cur.execute(f"SELECT nextval('{META_SCHEMA}.change_log_seq')").fetchone()[0]
        cur.execute(
            f"INSERT INTO {qualified([META_SCHEMA, 'change_log'])} "
            "(id, seq, schema_name, table_name, object_type, row_id, change_type, changed_at, changed_by, "
            "batch_id, before_json, after_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                seq,
                seq,
                form.function,
                form.name,
                "file" if isinstance(form, FileDef) else "form",
                row_id,
                change_type,
                when,
                actor.username,
                batch,
                json.dumps(before, default=_json_default) if before is not None else None,
                json.dumps(after, default=_json_default) if after is not None else None,
            ],
        )

    def _fetch_row(self, cur, form: FormDef, row_id: str) -> dict[str, Any] | None:
        cols = self._select_columns(form)
        select = ", ".join(quote_ident(c) for c in cols)
        row = cur.execute(
            f"SELECT {select} FROM {self._t(form)} WHERE {quote_ident(ID_COLUMN)} = ?", [row_id]
        ).fetchone()
        return dict(zip(cols, row, strict=True)) if row else None

    def apply_changes(self, form: FormDef, changes: ChangeSet, actor: User) -> SaveResult:
        if not form.is_editable:
            raise BackendError("This table has no system columns and cannot be edited row by row.")
        result = SaveResult()
        now = utcnow()
        batch = uuid.uuid4().hex
        user_cols = [c.name for c in form.user_columns]
        touched: list[str] = []  # rows whose current window closes
        opened: list[str] = []  # rows that get a new open window
        with self._tx() as cur:
            for ins in changes.inserts:
                row_id = new_row_id()
                values = {c: to_db_scalar(ins.values.get(c)) for c in user_cols}
                cols = [
                    ID_COLUMN,
                    VERSION_COLUMN,
                    CREATED_AT_COLUMN,
                    CREATED_BY_COLUMN,
                    UPDATED_AT_COLUMN,
                    UPDATED_BY_COLUMN,
                    *user_cols,
                ]
                params = [
                    row_id,
                    1,
                    now,
                    actor.username,
                    now,
                    actor.username,
                    *[values[c] for c in user_cols],
                ]
                cur.execute(
                    f"INSERT INTO {self._t(form)} ({', '.join(quote_ident(c) for c in cols)}) "
                    f"VALUES ({', '.join('?' for _ in cols)})",
                    params,
                )
                self._log(cur, form, row_id, "insert", actor, batch, None, {ID_COLUMN: row_id, **values}, now)
                result.inserted += 1
                opened.append(row_id)
            for upd in changes.updates:
                values = {c: to_db_scalar(v) for c, v in upd.changes.items() if c in user_cols}
                if not values:
                    continue
                before = self._fetch_row(cur, form, upd.row_id)
                set_clause = ", ".join(f"{quote_ident(c)} = ?" for c in values)
                params = [
                    *values.values(),
                    now,
                    actor.username,
                    upd.row_id,
                    to_db_scalar(upd.expected_version),
                ]
                n = cur.execute(
                    f"UPDATE {self._t(form)} SET {set_clause}, {quote_ident(UPDATED_AT_COLUMN)} = ?, "
                    f"{quote_ident(UPDATED_BY_COLUMN)} = ?, {quote_ident(VERSION_COLUMN)} = {quote_ident(VERSION_COLUMN)} + 1 "
                    f"WHERE {quote_ident(ID_COLUMN)} = ? AND {quote_ident(VERSION_COLUMN)} IS NOT DISTINCT FROM ?",
                    params,
                ).fetchone()[0]
                if n == 0:
                    result.conflicts.append(self._conflict_message(upd.label or upd.row_id, before))
                    continue
                after = self._fetch_row(cur, form, upd.row_id)
                self._log(cur, form, upd.row_id, "update", actor, batch, before, after, now)
                result.updated += 1
                touched.append(upd.row_id)
                opened.append(upd.row_id)
            for dele in changes.deletes:
                before = self._fetch_row(cur, form, dele.row_id)
                n = cur.execute(
                    f"DELETE FROM {self._t(form)} WHERE {quote_ident(ID_COLUMN)} = ? "
                    f"AND {quote_ident(VERSION_COLUMN)} IS NOT DISTINCT FROM ?",
                    [dele.row_id, to_db_scalar(dele.expected_version)],
                ).fetchone()[0]
                if n == 0:
                    result.conflicts.append(self._conflict_message(dele.label or dele.row_id, before))
                    continue
                self._log(cur, form, dele.row_id, "delete", actor, batch, before, None, now)
                result.deleted += 1
                touched.append(dele.row_id)
            if form.scd2_enabled and (touched or opened):
                self._scd2_close(cur, form, touched, now)
                self._scd2_open(cur, form, opened, now)
        return result

    # -- SCD Type 2 history table (FR-47) ---------------------------------------------------

    def _h(self, form: FormDef) -> str:
        return qualified([form.function, scd2_table_name(form.name)])

    def _scd2_columns(self, cur, form: FormDef) -> list[str]:
        """The form's columns as stored, so history follows schema changes (add/drop column)."""
        return [c.name for c in self._load_columns(cur, form.function, form.name)]

    def _scd2_create(self, cur, form: FormDef) -> None:
        """Create the history table, or bring an existing one back in step with the form.

        The history table outlives the flag, so it can fall behind the form: turning SCD2
        off, adding or removing a column and turning it on again used to leave a table whose
        shape no longer matched, and every later save failed. Missing columns are added here
        (never dropped - a column removed from the form keeps its recorded history).
        """
        history = scd2_table_name(form.name)
        columns = self._load_columns(cur, form.function, form.name)
        if not self._table_exists(cur, form.function, history):
            cols = ", ".join(quote_ident(c.name) for c in columns)
            cur.execute(
                f"CREATE TABLE {self._h(form)} AS "
                f"SELECT {cols}, CAST(NULL AS TIMESTAMP) AS \"{SCD2_START_COLUMN}\", "
                f"CAST(NULL AS TIMESTAMP) AS \"{SCD2_END_COLUMN}\" FROM {self._t(form)} WHERE 1 = 0"
            )
            return
        present = {c.name for c in self._load_columns(cur, form.function, history)}
        for col in columns:
            if col.name not in present:
                cur.execute(
                    f"ALTER TABLE {self._h(form)} ADD COLUMN {quote_ident(col.name)} "
                    f"{col.native_type or native_type_duckdb(col)}"
                )

    def _scd2_open(self, cur, form: FormDef, row_ids: list[str], now: datetime) -> None:
        """One new open window per row, valid from ``now`` (the save/backfill timestamp)."""
        if not row_ids:
            return
        names = self._scd2_columns(cur, form)
        cols = ", ".join(quote_ident(c) for c in names)
        # The target columns are named, so a history table that still carries a column the
        # form has dropped keeps working (that column is simply left NULL).
        target = ", ".join([*(quote_ident(c) for c in names), f'"{SCD2_START_COLUMN}"', f'"{SCD2_END_COLUMN}"'])
        markers = ", ".join("?" for _ in row_ids)
        cur.execute(
            f"INSERT INTO {self._h(form)} ({target}) SELECT {cols}, ?, NULL FROM {self._t(form)} "
            f"WHERE {quote_ident(ID_COLUMN)} IN ({markers})",
            [now, *row_ids],
        )

    def _scd2_close(self, cur, form: FormDef, row_ids: list[str], now: datetime) -> None:
        if not row_ids:
            return
        markers = ", ".join("?" for _ in row_ids)
        cur.execute(
            f"UPDATE {self._h(form)} SET \"{SCD2_END_COLUMN}\" = ? "
            f"WHERE \"{SCD2_END_COLUMN}\" IS NULL AND {quote_ident(ID_COLUMN)} IN ({markers})",
            [now, *row_ids],
        )

    def set_scd2(self, form: FormDef, enabled: bool, actor: User) -> FormDef:
        scd2_table_name(form.name)  # length check before anything is written
        with self._tx() as cur:
            if not self._table_exists(cur, form.function, form.name):
                raise NotFoundError(f"Form '{form.full_name}' does not exist.")
            if enabled:
                self._scd2_create(cur, form)
                ids = [
                    r[0]
                    for r in cur.execute(
                        f"SELECT {quote_ident(ID_COLUMN)} FROM {self._t(form)} WHERE {quote_ident(ID_COLUMN)} NOT IN "
                        f"(SELECT {quote_ident(ID_COLUMN)} FROM {self._h(form)} WHERE \"{SCD2_END_COLUMN}\" IS NULL)"
                    ).fetchall()
                ]
                self._scd2_open(cur, form, [str(i) for i in ids], utcnow())
            elif self._table_exists(cur, form.function, scd2_table_name(form.name)):
                # Close every open window. ``__END_AT IS NULL`` is published to consumers as
                # "this is the current version", so leaving windows open after the app stops
                # maintaining them would advertise values the form no longer holds. Closing
                # them says "tracking stopped here", and makes a later re-enable open a fresh
                # window for every current row instead of skipping the ones still open.
                cur.execute(
                    f'UPDATE {self._h(form)} SET "{SCD2_END_COLUMN}" = ? '
                    f'WHERE "{SCD2_END_COLUMN}" IS NULL',
                    [utcnow()],
                )
            self._set_props(
                cur, "table", form.function, form.name, {PROP_SCD2: "true" if enabled else ""}
            )
        return self.get_form(form.function, form.name)

    @staticmethod
    def _conflict_message(label: str, current: dict[str, Any] | None) -> str:
        if current is None:
            return f"{label}: the row was deleted by someone else."
        who = current.get(UPDATED_BY_COLUMN) or "someone else"
        when = current.get(UPDATED_AT_COLUMN)
        when_txt = f" at {when:%Y-%m-%d %H:%M:%S} UTC" if isinstance(when, datetime) else ""
        return f"{label}: modified by {who}{when_txt} after you loaded it."

    def append_rows(self, form: FormDef, rows: pd.DataFrame, actor: User) -> int:
        if not form.is_editable:
            raise BackendError("This table has no system columns and cannot be edited.")
        with self._tx() as cur:
            return self._append_rows(cur, form, rows, actor)

    def _append_rows(self, cur, form: FormDef, rows: pd.DataFrame, actor: User) -> int:
        if rows is None or rows.empty:
            return 0
        now = utcnow()
        batch = uuid.uuid4().hex
        user_cols = [c.name for c in form.user_columns]
        df = pd.DataFrame({c: (rows[c] if c in rows.columns else None) for c in user_cols})
        df = df.astype(object).where(df.notna(), None)
        ids = [new_row_id() for _ in range(len(df))]
        df.insert(0, ID_COLUMN, ids)
        df.insert(1, VERSION_COLUMN, 1)
        df.insert(2, CREATED_AT_COLUMN, now)
        df.insert(3, CREATED_BY_COLUMN, actor.username)
        df.insert(4, UPDATED_AT_COLUMN, now)
        df.insert(5, UPDATED_BY_COLUMN, actor.username)
        for c in form.user_columns:
            if c.name in df.columns and c.data_type is not DataType.OTHER:
                df[c.name] = df[c.name].map(lambda v, col=c: _coerce_for_import(col, v)).astype(object)
                df[c.name] = df[c.name].where(df[c.name].notna(), None)
        view = f"rdm_import_{batch}"
        cur.register(view, df)
        col_list = ", ".join(quote_ident(c) for c in df.columns)
        try:
            cur.execute(f"INSERT INTO {self._t(form)} ({col_list}) SELECT {col_list} FROM {view}")
        except duckdb.Error as exc:
            raise BackendError(f"Import failed: {exc}") from exc
        finally:
            cur.unregister(view)
        date_cols = {c.name for c in form.user_columns if c.data_type is DataType.DATE}
        for rec in df.to_dict("records"):
            after = {k: to_db_scalar(v) for k, v in rec.items() if k in user_cols or k == ID_COLUMN}
            for k in date_cols:
                if isinstance(after.get(k), datetime):
                    after[k] = after[k].date()
            self._log(cur, form, rec[ID_COLUMN], "insert", actor, batch, None, after, now)
        if form.scd2_enabled:
            self._scd2_open(cur, form, ids, now)
        return len(df)

    def get_history(self, form: FormDef, limit: int = 200, row_id: str | None = None) -> pd.DataFrame:
        params: list[Any] = [form.function, form.name]
        row_filter = ""
        if row_id:
            row_filter = " AND row_id = ?"
            params.append(row_id)
        params.append(int(limit))
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT changed_at, changed_by, change_type, row_id, before_json, after_json "
                f"FROM {qualified([META_SCHEMA, 'change_log'])} WHERE schema_name = ? AND table_name = ? "
                "AND (object_type = 'form' OR object_type IS NULL)"
                f"{row_filter} ORDER BY seq DESC LIMIT ?",
                params,
            ).fetchall()
        user_cols = [c.name for c in form.user_columns]
        records = []
        n = len(rows)
        for i, (changed_at, changed_by, change_type, rid, before_json, after_json) in enumerate(rows):
            version = n - i  # the entry's position in the history shown, as on Databricks
            before = json.loads(before_json) if before_json else {}
            after = json.loads(after_json) if after_json else {}
            snapshot = after if change_type != "delete" else before
            changed = [c for c in user_cols if change_type == "update" and before.get(c) != after.get(c)]
            rec = {
                "version": version,
                "changed_at": changed_at,
                "changed_by": changed_by,
                "change_type": change_type,
                "changed_fields": ", ".join(changed),
                ID_COLUMN: rid,
            }
            for c in user_cols:
                rec[c] = snapshot.get(c)
            records.append(rec)
        columns = [*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, *user_cols]
        return pd.DataFrame.from_records(records, columns=columns)

    # -- files -------------------------------------------------------------------------------

    def _file_path(self, function: str, name: str) -> Path:
        validate_identifier(function, "function name")
        validate_file_name(name)
        return self.files_dir / function / name

    @staticmethod
    def _reader(path: Path, fmt: str) -> str:
        target = lit(path.as_posix())
        return f"read_parquet({target})" if fmt == "parquet" else f"read_csv_auto({target}, header = true)"

    def _file_from(self, function: str, name: str, reg: dict[str, Any] | None) -> FileDef:
        path = self._file_path(function, name)
        reg = reg or {}
        size = path.stat().st_size if path.is_file() else reg.get("size_bytes")
        return FileDef(
            function=function,
            name=name,
            display_name=reg.get("display_name") or "",
            description=reg.get("description") or "",
            owner=reg.get("owner") or "",
            owner_email=reg.get("owner_email") or "",
            size_bytes=int(size) if size is not None else None,
            row_count=int(reg["row_count"]) if reg.get("row_count") is not None else None,
            path=path.as_posix(),
            registered=bool(reg),
            created_at=reg.get("created_at"),
            created_by=reg.get("created_by") or "",
            updated_at=reg.get("updated_at"),
            updated_by=reg.get("updated_by") or "",
        )

    def list_files(self, function: str) -> list[FileDef]:
        with self._cursor() as cur:
            if not self._schema_exists(cur, function):
                raise NotFoundError(f"Function '{function}' does not exist.")
            registry = self._file_registry(cur, function)
        names = sorted(set(self._stored_file_names(function)) | set(registry))
        return [
            self._file_from(function, n, registry.get(n))
            for n in names
            if self._file_path(function, n).is_file()
        ]

    def get_file(self, function: str, name: str) -> FileDef:
        path = self._file_path(function, name)
        if not path.is_file():
            raise NotFoundError(f"File '{function}/{name}' does not exist.")
        with self._cursor() as cur:
            reg = self._file_registry(cur, function).get(name)
        return self._file_from(function, name, reg)

    def _count_file_rows(self, cur, path: Path, fmt: str) -> int:
        try:
            return int(cur.execute(f"SELECT count(*) FROM {self._reader(path, fmt)}").fetchone()[0])
        except duckdb.Error as exc:
            raise BackendError(f"The file cannot be read as {fmt.upper()}: {exc}") from exc

    def put_file(self, file: FileDef, data: bytes, actor: User, replace: bool = False) -> FileDef:
        file.validate()
        path = self._file_path(file.function, file.name)
        existed = path.is_file()
        if existed and not replace:
            raise ConflictError(f"File '{file.full_name}' already exists. Replace it from its page instead.")
        if replace and not existed:
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        with self._tx() as cur:
            if not self._schema_exists(cur, file.function):
                raise NotFoundError(f"Function '{file.function}' does not exist.")
            previous = (
                self._file_from(
                    file.function, file.name, self._file_registry(cur, file.function).get(file.name)
                )
                if existed
                else None
            )
            backup = path.read_bytes() if existed else None
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            try:
                row_count = self._count_file_rows(cur, path, file.format)
            except BackendError:
                if backup is not None:
                    path.write_bytes(backup)
                else:
                    path.unlink(missing_ok=True)
                raise
            file.size_bytes = len(data)
            file.row_count = row_count
            if previous is not None:
                file.display_name = file.display_name or previous.display_name
                file.description = file.description or previous.description
                file.owner = file.owner or previous.owner
                file.owner_email = file.owner_email or previous.owner_email
            file.owner = file.owner or actor.username
            self._register_file(cur, file, actor)
            self._log(
                cur,
                file,
                None,
                "replace" if existed else "upload",
                actor,
                uuid.uuid4().hex,
                {"size_bytes": previous.size_bytes, "row_count": previous.row_count} if previous else None,
                {"size_bytes": file.size_bytes, "row_count": file.row_count, "format": file.format},
                utcnow(),
            )
        return self.get_file(file.function, file.name)

    def update_file_metadata(self, file: FileDef, actor: User) -> FileDef:
        file.validate()
        current = self.get_file(file.function, file.name)
        current.display_name, current.description, current.owner, current.owner_email = (
            file.display_name,
            file.description,
            file.owner,
            file.owner_email,
        )
        with self._tx() as cur:
            self._register_file(cur, current, actor)
        return self.get_file(file.function, file.name)

    def read_file(self, file: FileDef) -> bytes:
        path = self._file_path(file.function, file.name)
        if not path.is_file():
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        return path.read_bytes()

    def preview_file(self, file: FileDef, limit: int = 100) -> pd.DataFrame:
        path = self._file_path(file.function, file.name)
        if not path.is_file():
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        with self._cursor() as cur:
            try:
                return cur.execute(
                    f"SELECT * FROM {self._reader(path, file.format)} LIMIT ?", [int(limit)]
                ).df()
            except duckdb.Error as exc:
                raise BackendError(f"The file cannot be read as {file.format.upper()}: {exc}") from exc

    def file_columns(self, file: FileDef) -> list[ColumnDef]:
        path = self._file_path(file.function, file.name)
        if not path.is_file():
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        with self._cursor() as cur:
            try:
                rows = cur.execute(f"DESCRIBE SELECT * FROM {self._reader(path, file.format)}").fetchall()
            except duckdb.Error as exc:
                raise BackendError(f"The file cannot be read as {file.format.upper()}: {exc}") from exc
        cols = []
        for i, row in enumerate(rows):
            cname, dtype = str(row[0]), str(row[1])
            t, p, s = parse_native_type(dtype)
            cols.append(
                ColumnDef(name=cname, data_type=t, precision=p, scale=s, native_type=dtype, position=i)
            )
        return cols

    def file_history(self, file: FileDef, limit: int = 200) -> pd.DataFrame:
        placeholders = ", ".join("?" for _ in FILE_CHANGE_TYPES)
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT changed_at, changed_by, change_type, before_json, after_json "
                f"FROM {qualified([META_SCHEMA, 'change_log'])} WHERE schema_name = ? AND table_name = ? "
                f"AND change_type IN ({placeholders}) ORDER BY seq DESC LIMIT ?",
                [file.function, file.name, *FILE_CHANGE_TYPES, int(limit)],
            ).fetchall()
        records = []
        n = len(rows)
        for i, (changed_at, changed_by, change_type, before_json, after_json) in enumerate(rows):
            version = n - i
            snapshot = (
                json.loads(after_json) if after_json else (json.loads(before_json) if before_json else {})
            )
            records.append(
                {
                    "version": version,
                    "changed_at": changed_at,
                    "changed_by": changed_by,
                    "change_type": change_type,
                    "size_bytes": snapshot.get("size_bytes"),
                    "row_count": snapshot.get("row_count"),
                }
            )
        return pd.DataFrame.from_records(
            records, columns=["version", "changed_at", "changed_by", "change_type", "size_bytes", "row_count"]
        )

    def drop_file(self, file: FileDef, actor: User) -> None:
        path = self._file_path(file.function, file.name)
        if not path.is_file():
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        with self._tx() as cur:
            current = self._file_from(
                file.function, file.name, self._file_registry(cur, file.function).get(file.name)
            )
            self._unregister_file(cur, file.function, file.name)
            self._log(
                cur,
                file,
                None,
                "delete",
                actor,
                uuid.uuid4().hex,
                {"size_bytes": current.size_bytes, "row_count": current.row_count, "format": file.format},
                None,
                utcnow(),
            )
            path.unlink()

    # -- authorisation -------------------------------------------------------------------

    def get_permissions(self, user: User) -> Permissions:
        principals = list(user.principals)
        if not principals:
            return Permissions()
        with self._cursor() as cur:
            placeholders = ", ".join("?" for _ in principals)
            rows = cur.execute(
                f"SELECT schema_name, role FROM {qualified([META_SCHEMA, 'grants'])} WHERE principal IN ({placeholders})",
                principals,
            ).fetchall()
            functions = [
                r[0]
                for r in cur.execute("SELECT schema_name FROM duckdb_schemas() WHERE NOT internal").fetchall()
                if r[0] not in HIDDEN_SCHEMAS
            ]
        catalog_role = Role.NONE
        per_function: dict[str, Role] = {}
        for schema, role_name in rows:
            role = Role.from_name(role_name)
            if schema == CATALOG_LEVEL:
                catalog_role = max(catalog_role, role)
            else:
                per_function[schema] = max(per_function.get(schema, Role.NONE), role)
        roles = {f: max(per_function.get(f, Role.NONE), catalog_role) for f in functions}
        return Permissions(function_roles=roles, is_global_admin=catalog_role.can_admin)

    def list_function_grants(self, function: str) -> list[tuple[str, Role]]:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT principal, role FROM {qualified([META_SCHEMA, 'grants'])} WHERE schema_name = ? ORDER BY principal",
                [function],
            ).fetchall()
        return [(p, Role.from_name(r)) for p, r in rows]

    def list_groups(self, query: str | None = None) -> list[str]:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT DISTINCT principal FROM {qualified([META_SCHEMA, 'grants'])}"
            ).fetchall()
        groups = set(DEFAULT_LOCAL_GROUPS) | {r[0] for r in rows if "@" not in r[0]}
        needle = (query or "").strip().lower()
        return sorted(g for g in groups if needle in g.lower())

    def grant_function_role(self, function: str, principal: str, role: Role, actor: User) -> None:
        principal = (principal or "").strip()
        if not principal:
            raise BackendError("A group is required.")
        if "@" in principal:
            raise BackendError("Access is granted to groups only, not to individual users.")
        with self._tx() as cur:
            if function != CATALOG_LEVEL and not self._schema_exists(cur, function):
                raise NotFoundError(f"Function '{function}' does not exist.")
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'grants'])} WHERE schema_name = ? AND principal = ?",
                [function, principal],
            )
            if role is not Role.NONE:
                cur.execute(
                    f"INSERT INTO {qualified([META_SCHEMA, 'grants'])} "
                    "(schema_name, principal, role) VALUES (?, ?, ?)",
                    [function, principal, role.name],
                )
