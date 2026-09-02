"""DuckDB implementation of :class:`DatabaseBackend` for local development and tests.

DuckDB gives us real DDL/DML, ``COMMENT ON``, ``information_schema`` and transactions.
What Unity Catalog has and DuckDB lacks is emulated in a private ``_rdm_meta`` schema:

* ``object_properties`` - table properties / tags / schema comments
* ``grants``           - schema-level roles for principals (groups or users)
* ``change_log``       - row-level history (Delta Change Data Feed equivalent)
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from rdm.backend.base import BackendError, ConflictError, DatabaseBackend, NotFoundError
from rdm.backend.sql_utils import escape_literal_duckdb as lit
from rdm.backend.sql_utils import (
    native_type_duckdb,
    normalise_frame,
    parse_native_type,
    qualified,
    quote_ident,
    to_db_scalar,
)
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
    FormDef,
    Permissions,
    Role,
    SaveResult,
    User,
    new_row_id,
    system_columns,
)

log = logging.getLogger(__name__)

META_SCHEMA = "_rdm_meta"
HIDDEN_SCHEMAS = frozenset({"main", "information_schema", "pg_catalog", "temp", META_SCHEMA})
CATALOG_LEVEL = "*"


def utcnow() -> datetime:
    """Naive UTC timestamps are the app's canonical representation."""
    return datetime.now(UTC).replace(tzinfo=None)


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal | date | datetime | pd.Timestamp):
        return str(value)
    if hasattr(value, "item"):
        return value.item()
    return str(value)


class DuckDBBackend(DatabaseBackend):
    name = "duckdb"

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
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
        with self._cursor() as cur:
            cur.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_ident(META_SCHEMA)}")
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
            cur.execute(
                f"""CREATE TABLE IF NOT EXISTS {qualified([META_SCHEMA, "change_log"])} (
                        id          BIGINT PRIMARY KEY,
                        schema_name VARCHAR NOT NULL,
                        table_name  VARCHAR NOT NULL,
                        row_id      VARCHAR,
                        change_type VARCHAR NOT NULL,
                        changed_at  TIMESTAMP NOT NULL,
                        changed_by  VARCHAR,
                        batch_id    VARCHAR,
                        before_json VARCHAR,
                        after_json  VARCHAR)"""
            )

    @staticmethod
    def _t(form: FormDef) -> str:
        return qualified([form.domain, form.name])

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
                    f"INSERT INTO {qualified([META_SCHEMA, 'object_properties'])} VALUES (?, ?, ?, ?, ?)",
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

    # -- domains -----------------------------------------------------------------------------

    def _schema_exists(self, cur, name: str) -> bool:
        row = cur.execute(
            "SELECT 1 FROM duckdb_schemas() WHERE NOT internal AND schema_name = ?", [name]
        ).fetchone()
        return row is not None

    def list_domains(self) -> list[DomainDef]:
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
                    "GROUP BY schema_name"
                ).fetchall()
            )
            return [self._domain_from(cur, n, counts.get(n, 0)) for n in names]

    def _domain_from(self, cur, name: str, form_count: int | None) -> DomainDef:
        props = self._get_props(cur, "schema", name)
        return DomainDef(
            name=name,
            display_name=props.get(PROP_DISPLAY_NAME, ""),
            description=props.get("comment", ""),
            owner=props.get(PROP_OWNER, ""),
            form_count=form_count,
            properties=props,
        )

    def get_domain(self, name: str) -> DomainDef:
        with self._cursor() as cur:
            if name in HIDDEN_SCHEMAS or not self._schema_exists(cur, name):
                raise NotFoundError(f"Domain '{name}' does not exist.")
            count = cur.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE NOT internal AND NOT temporary AND schema_name = ?",
                [name],
            ).fetchone()[0]
            return self._domain_from(cur, name, count)

    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        if domain.name in HIDDEN_SCHEMAS:
            raise ConflictError(f"'{domain.name}' is a reserved name.")
        with self._tx() as cur:
            if self._schema_exists(cur, domain.name):
                raise ConflictError(f"Domain '{domain.name}' already exists.")
            cur.execute(f"CREATE SCHEMA {quote_ident(domain.name)}")
            self._set_props(
                cur,
                "schema",
                domain.name,
                "",
                {
                    "comment": domain.description,
                    PROP_DISPLAY_NAME: domain.display_name,
                    PROP_OWNER: domain.owner or actor.username,
                    "created_by": actor.username,
                },
            )
            # The creator administers the new domain (Unity Catalog makes the creator the owner).
            cur.execute(
                f"INSERT OR REPLACE INTO {qualified([META_SCHEMA, 'grants'])} VALUES (?, ?, ?)",
                [domain.name, actor.username, Role.ADMIN.name],
            )
        return self.get_domain(domain.name)

    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        with self._tx() as cur:
            if not self._schema_exists(cur, domain.name):
                raise NotFoundError(f"Domain '{domain.name}' does not exist.")
            self._set_props(
                cur,
                "schema",
                domain.name,
                "",
                {
                    "comment": domain.description,
                    PROP_DISPLAY_NAME: domain.display_name,
                    PROP_OWNER: domain.owner,
                },
            )
        return self.get_domain(domain.name)

    # -- forms -------------------------------------------------------------------------------

    def list_forms(self, domain: str) -> list[FormDef]:
        with self._cursor() as cur:
            if not self._schema_exists(cur, domain):
                raise NotFoundError(f"Domain '{domain}' does not exist.")
            rows = cur.execute(
                "SELECT table_name, comment, estimated_size FROM duckdb_tables() "
                "WHERE NOT internal AND NOT temporary AND schema_name = ? ORDER BY table_name",
                [domain],
            ).fetchall()
            props = self._get_props_bulk(cur, "table", domain)
            tags = self._get_props_bulk(cur, "table_tag", domain)
            forms = []
            for name, comment, size in rows:
                p = props.get(name, {})
                forms.append(
                    FormDef(
                        domain=domain,
                        name=name,
                        display_name=p.get(PROP_DISPLAY_NAME, ""),
                        description=comment or "",
                        owner=p.get(PROP_OWNER, ""),
                        properties=p,
                        tags=tags.get(name, {}),
                        row_count=int(size) if size is not None else None,
                    )
                )
            return forms

    def _table_exists(self, cur, domain: str, name: str) -> bool:
        row = cur.execute(
            "SELECT 1 FROM duckdb_tables() WHERE NOT internal AND NOT temporary AND schema_name = ? AND table_name = ?",
            [domain, name],
        ).fetchone()
        return row is not None

    def _load_columns(self, cur, domain: str, name: str) -> list[ColumnDef]:
        rows = cur.execute(
            "SELECT column_name, data_type, is_nullable, comment, column_index FROM duckdb_columns() "
            "WHERE NOT internal AND schema_name = ? AND table_name = ? ORDER BY column_index",
            [domain, name],
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

    def get_form(self, domain: str, name: str) -> FormDef:
        with self._cursor() as cur:
            if not self._table_exists(cur, domain, name):
                raise NotFoundError(f"Form '{domain}.{name}' does not exist.")
            comment = cur.execute(
                "SELECT comment FROM duckdb_tables() WHERE schema_name = ? AND table_name = ?", [domain, name]
            ).fetchone()[0]
            props = self._get_props(cur, "table", domain, name)
            tags = self._get_props(cur, "table_tag", domain, name)
            form = FormDef(
                domain=domain,
                name=name,
                display_name=props.get(PROP_DISPLAY_NAME, ""),
                description=comment or "",
                owner=props.get(PROP_OWNER, ""),
                columns=self._load_columns(cur, domain, name),
                properties=props,
                tags=tags,
            )
            form.apply_column_config(props.get(PROP_COLUMN_CONFIG))
            form.row_count = cur.execute(f"SELECT count(*) FROM {self._t(form)}").fetchone()[0]
            if form.has_system_columns:
                row = cur.execute(
                    f"SELECT min({quote_ident(CREATED_AT_COLUMN)}), max({quote_ident(UPDATED_AT_COLUMN)}) FROM {self._t(form)}"
                ).fetchone()
                form.created_at, form.updated_at = row[0], row[1]
                if form.updated_at is not None:
                    who = cur.execute(
                        f"SELECT {quote_ident(UPDATED_BY_COLUMN)} FROM {self._t(form)} "
                        f"WHERE {quote_ident(UPDATED_AT_COLUMN)} = ? LIMIT 1",
                        [form.updated_at],
                    ).fetchone()
                    form.updated_by = (who[0] if who else "") or ""
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
            if not self._schema_exists(cur, form.domain):
                raise NotFoundError(f"Domain '{form.domain}' does not exist.")
            if self._table_exists(cur, form.domain, form.name):
                raise ConflictError(f"Form '{form.full_name}' already exists.")
            col_ddl = ",\n  ".join(self._column_ddl(c) for c in form.columns)
            cur.execute(
                f"CREATE TABLE {self._t(form)} (\n  {col_ddl},\n  PRIMARY KEY ({quote_ident(ID_COLUMN)}))"
            )
            self._write_comments(cur, form)
            self._set_props(
                cur,
                "table",
                form.domain,
                form.name,
                {
                    PROP_FORM: "true",
                    PROP_DISPLAY_NAME: form.display_name,
                    PROP_OWNER: form.owner or actor.username,
                    PROP_COLUMN_CONFIG: form.column_config_json(),
                    "created_by": actor.username,
                    "created_at": utcnow().isoformat(),
                },
            )
            self._set_props(
                cur,
                "table_tag",
                form.domain,
                form.name,
                {
                    "rdm_form": "true",
                    "rdm_display_name": form.display_name,
                    "rdm_owner": form.owner or actor.username,
                },
            )
            if rows is not None and len(rows):
                self._append_rows(cur, form, rows, actor)
        return self.get_form(form.domain, form.name)

    def _write_comments(self, cur, form: FormDef) -> None:
        cur.execute(f"COMMENT ON TABLE {self._t(form)} IS {lit(form.description or '')}")
        for c in form.columns:
            cur.execute(
                f"COMMENT ON COLUMN {self._t(form)}.{quote_ident(c.name)} IS {lit(c.description or '')}"
            )

    def update_form_metadata(self, form: FormDef, actor: User) -> FormDef:
        form.validate()
        with self._tx() as cur:
            if not self._table_exists(cur, form.domain, form.name):
                raise NotFoundError(f"Form '{form.full_name}' does not exist.")
            existing = {c.name: c for c in self._load_columns(cur, form.domain, form.name)}
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
                form.domain,
                form.name,
                {
                    PROP_DISPLAY_NAME: form.display_name,
                    PROP_OWNER: form.owner,
                    PROP_COLUMN_CONFIG: form.column_config_json(),
                },
            )
            self._set_props(
                cur,
                "table_tag",
                form.domain,
                form.name,
                {"rdm_display_name": form.display_name, "rdm_owner": form.owner},
            )
        return self.get_form(form.domain, form.name)

    def add_column(self, form: FormDef, column: ColumnDef, actor: User) -> FormDef:
        column.validate()
        if column.is_system:
            raise BackendError("System columns cannot be added manually.")
        with self._tx() as cur:
            if form.column(column.name) is not None or column.name in {
                c.name for c in self._load_columns(cur, form.domain, form.name)
            }:
                raise ConflictError(f"Column '{column.name}' already exists.")
            cur.execute(
                f"ALTER TABLE {self._t(form)} ADD COLUMN {quote_ident(column.name)} {native_type_duckdb(column)}"
            )
            cur.execute(
                f"COMMENT ON COLUMN {self._t(form)}.{quote_ident(column.name)} IS {lit(column.description or '')}"
            )
            column.nullable = True  # existing rows have no value; NOT NULL can be set later
            form.columns.append(column)
            self._set_props(
                cur, "table", form.domain, form.name, {PROP_COLUMN_CONFIG: form.column_config_json()}
            )
        return self.get_form(form.domain, form.name)

    def drop_column(self, form: FormDef, column_name: str, actor: User) -> FormDef:
        if column_name in SYSTEM_COLUMNS:
            raise BackendError("System columns cannot be removed.")
        with self._tx() as cur:
            names = {c.name for c in self._load_columns(cur, form.domain, form.name)}
            if column_name not in names:
                raise NotFoundError(f"Column '{column_name}' does not exist.")
            cur.execute(f"ALTER TABLE {self._t(form)} DROP COLUMN {quote_ident(column_name)}")
            form.columns = [c for c in form.columns if c.name != column_name]
            self._set_props(
                cur, "table", form.domain, form.name, {PROP_COLUMN_CONFIG: form.column_config_json()}
            )
        return self.get_form(form.domain, form.name)

    def drop_form(self, form: FormDef, actor: User) -> None:
        with self._tx() as cur:
            if not self._table_exists(cur, form.domain, form.name):
                raise NotFoundError(f"Form '{form.full_name}' does not exist.")
            cur.execute(f"DROP TABLE {self._t(form)}")
            self._delete_props(cur, form.domain, form.name)
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'change_log'])} WHERE schema_name = ? AND table_name = ?",
                [form.domain, form.name],
            )

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
        cur.execute(
            f"INSERT INTO {qualified([META_SCHEMA, 'change_log'])} "
            f"VALUES (nextval('{META_SCHEMA}.change_log_seq'), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                form.domain,
                form.name,
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
        return result

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
        for c in form.columns:
            if (
                c.data_type in (DataType.DATE, DataType.TIMESTAMP)
                and c.name in df.columns
                and not c.is_system
            ):
                df[c.name] = pd.to_datetime(df[c.name], errors="coerce")
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
        return len(df)

    def get_history(self, form: FormDef, limit: int = 200) -> pd.DataFrame:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT id, changed_at, changed_by, change_type, row_id, before_json, after_json "
                f"FROM {qualified([META_SCHEMA, 'change_log'])} WHERE schema_name = ? AND table_name = ? "
                "ORDER BY id DESC LIMIT ?",
                [form.domain, form.name, int(limit)],
            ).fetchall()
        user_cols = [c.name for c in form.user_columns]
        records = []
        for version, changed_at, changed_by, change_type, row_id, before_json, after_json in rows:
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
                ID_COLUMN: row_id,
            }
            for c in user_cols:
                rec[c] = snapshot.get(c)
            records.append(rec)
        columns = [*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, *user_cols]
        return pd.DataFrame.from_records(records, columns=columns)

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
            domains = [
                r[0]
                for r in cur.execute("SELECT schema_name FROM duckdb_schemas() WHERE NOT internal").fetchall()
                if r[0] not in HIDDEN_SCHEMAS
            ]
        catalog_role = Role.NONE
        per_domain: dict[str, Role] = {}
        for schema, role_name in rows:
            role = Role[role_name]
            if schema == CATALOG_LEVEL:
                catalog_role = max(catalog_role, role)
            else:
                per_domain[schema] = max(per_domain.get(schema, Role.NONE), role)
        roles = {d: max(per_domain.get(d, Role.NONE), catalog_role) for d in domains}
        return Permissions(domain_roles=roles, can_create_domain=catalog_role.can_admin)

    def list_domain_grants(self, domain: str) -> list[tuple[str, Role]]:
        with self._cursor() as cur:
            rows = cur.execute(
                f"SELECT principal, role FROM {qualified([META_SCHEMA, 'grants'])} WHERE schema_name = ? ORDER BY principal",
                [domain],
            ).fetchall()
        return [(p, Role[r]) for p, r in rows]

    def grant_domain_role(self, domain: str, principal: str, role: Role, actor: User) -> None:
        if not principal or not principal.strip():
            raise BackendError("A principal (group or user) is required.")
        with self._tx() as cur:
            if domain != CATALOG_LEVEL and not self._schema_exists(cur, domain):
                raise NotFoundError(f"Domain '{domain}' does not exist.")
            cur.execute(
                f"DELETE FROM {qualified([META_SCHEMA, 'grants'])} WHERE schema_name = ? AND principal = ?",
                [domain, principal.strip()],
            )
            if role is not Role.NONE:
                cur.execute(
                    f"INSERT INTO {qualified([META_SCHEMA, 'grants'])} VALUES (?, ?, ?)",
                    [domain, principal.strip(), role.name],
                )
