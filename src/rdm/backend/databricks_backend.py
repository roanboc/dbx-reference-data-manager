"""Databricks SQL warehouse implementation of :class:`DatabaseBackend`.

Production mapping (see docs/DESIGN.md):

* domain = Unity Catalog schema, form = Delta table in ``<catalog>``.
* Descriptions are ``COMMENT``s; display name / owner / column configuration live in
  ``TBLPROPERTIES ('rdm.*')`` and are mirrored to tags (``rdm_display_name``, ``rdm_owner``)
  so they are searchable in Catalog Explorer and readable in bulk from
  ``information_schema.table_tags``.
* A save is **one** ``MERGE`` statement whose source is a single JSON parameter
  (``inline(from_json(:payload, '<struct schema>'))``): atomic, idempotent on retry
  (app-generated ``_id``), no 255-parameter-marker limit. Concurrency uses the integer
  ``_version`` column.
* Row history is written to an explicit audit table ``<catalog>._rdm_meta.change_log``
  (same shape as the DuckDB backend); Delta Change Data Feed is enabled on every form for
  downstream SCD pipelines and used as a fallback when the audit table is unavailable.
* Roles are derived from Unity Catalog privileges inside the SQL session
  (``current_user()`` / ``is_account_group_member``), so with user authorization enabled
  the app renders exactly what the warehouse will allow.
* Domains and grants are infrastructure (Databricks Asset Bundle): the backend refuses to
  create schemas or to grant privileges.

All values are bound as parameters; identifiers are validated and back-quoted; DDL clauses
that cannot take parameters (comments, properties, tags) are escaped with Spark rules.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from rdm.backend.base import BackendError, ConflictError, DatabaseBackend, NotFoundError
from rdm.backend.sql_utils import escape_literal_databricks as lit
from rdm.backend.sql_utils import (
    native_type_databricks,
    normalise_frame,
    parse_native_type,
    to_db_scalar,
    validate_identifier,
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
    TAG_DISPLAY_NAME,
    TAG_FORM,
    TAG_OWNER,
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
AUDIT_TABLE = "change_log"
HIDDEN_SCHEMAS = frozenset({"information_schema", "default", META_SCHEMA})

#: Delta features every form is created with. Column mapping makes DROP/RENAME COLUMN
#: possible, deletion vectors give row-level concurrency, CDF feeds downstream SCD2.
FORM_TABLE_PROPERTIES = {
    "delta.enableChangeDataFeed": "true",
    "delta.columnMapping.mode": "name",
    "delta.enableDeletionVectors": "true",
}

#: Databricks SQL types used in ``from_json`` struct schemas and DDL.
JSON_ROWS_CHUNK = 500  # rows per MERGE/INSERT payload (keeps parameters well under 1 MB)
TS_FORMAT = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"

ADMIN_PRIVILEGES = {"MANAGE", "ALL PRIVILEGES", "ALL_PRIVILEGES", "OWN", "OWNER"}
CREATE_PRIVILEGES = {"CREATE TABLE", "CREATE_TABLE"}
EDIT_PRIVILEGES = {"MODIFY"}
VIEW_PRIVILEGES = {"SELECT"}


def utcnow() -> datetime:
    return datetime.now(UTC)


def _json_value(col: ColumnDef, value: Any) -> Any:
    """JSON representation understood by ``from_json`` for the column's Databricks type."""
    v = to_db_scalar(value)
    if v is None:
        return None
    t = col.data_type
    if t is DataType.DECIMAL:
        return str(Decimal(str(v)))
    if t is DataType.DATE:
        if isinstance(v, datetime):
            v = v.date()
        return v.isoformat() if isinstance(v, date) else str(v)
    if t is DataType.TIMESTAMP:
        if isinstance(v, date) and not isinstance(v, datetime):
            v = datetime(v.year, v.month, v.day)
        if isinstance(v, datetime):
            return v.strftime("%Y-%m-%dT%H:%M:%S.%f")
        return str(v)
    if t is DataType.BOOLEAN:
        return bool(v)
    if t is DataType.INTEGER:
        return int(v)
    if t is DataType.DOUBLE:
        return float(v)
    return str(v)


def _q(name: str) -> str:
    validate_identifier(name)
    return f"`{name}`"


class DatabricksBackend(DatabaseBackend):
    name = "databricks"

    def __init__(
        self,
        catalog: str,
        http_path: str,
        host: str | None = None,
        access_token: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        meta_schema: str = META_SCHEMA,
    ) -> None:
        validate_identifier(catalog, "catalog name")
        validate_identifier(meta_schema, "schema name")
        self.catalog = catalog
        self.http_path = http_path
        self.host = host
        self.access_token = access_token
        self.meta_schema = meta_schema
        self._connection_factory = connection_factory or self._connect
        self._conn: Any = None
        self._lock = threading.RLock()
        self._audit_ready: bool | None = None

    # -- connection --------------------------------------------------------------------------

    def describe(self) -> str:
        mode = "as you" if self.access_token else "as app service principal"
        return f"Databricks SQL ({self.http_path.rsplit('/', 1)[-1]}) · catalog {self.catalog} · {mode}"

    def _connect(self) -> Any:
        from databricks import sql
        from databricks.sdk.core import Config

        cfg = Config(host=self.host) if self.host else Config()
        hostname = (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
        if self.access_token:
            return sql.connect(
                server_hostname=hostname, http_path=self.http_path, access_token=self.access_token
            )
        return sql.connect(
            server_hostname=hostname, http_path=self.http_path, credentials_provider=lambda: cfg.authenticate
        )

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn = self._connection_factory()
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    def _run(
        self, statement: str, params: dict[str, Any] | list[Any] | None = None
    ) -> tuple[list[tuple], list[str]]:
        """Execute one statement; returns (rows, column names). Serialised per backend instance."""
        with self._lock:
            conn = self._connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(statement, params or None)
                    if cur.description:
                        cols = [d[0] for d in cur.description]
                        return list(cur.fetchall()), cols
                    return [], []
            except Exception as exc:  # noqa: BLE001 - wrap driver errors for the UI
                message = str(exc)
                log.warning("Databricks statement failed: %s\n%s", message[:500], statement[:500])
                lowered = message.lower()
                if "table_or_view_not_found" in lowered or "schema_not_found" in lowered:
                    raise NotFoundError(message) from exc
                if "already exists" in lowered:
                    raise ConflictError(message) from exc
                raise BackendError(message) from exc

    def _run_df(self, statement: str, params: dict[str, Any] | list[Any] | None = None) -> pd.DataFrame:
        with self._lock:
            conn = self._connection()
            try:
                with conn.cursor() as cur:
                    cur.execute(statement, params or None)
                    if hasattr(cur, "fetchall_arrow"):
                        try:
                            return cur.fetchall_arrow().to_pandas()
                        except Exception:  # noqa: BLE001 - fall back to row tuples
                            pass
                    cols = [d[0] for d in cur.description] if cur.description else []
                    return pd.DataFrame.from_records(list(cur.fetchall()), columns=cols)
            except BackendError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise BackendError(str(exc)) from exc

    # -- naming ------------------------------------------------------------------------------

    def _t(self, form: FormDef) -> str:
        return f"{_q(self.catalog)}.{_q(form.domain)}.{_q(form.name)}"

    def _s(self, domain: str) -> str:
        return f"{_q(self.catalog)}.{_q(domain)}"

    def _info(self, view: str) -> str:
        return f"{_q(self.catalog)}.`information_schema`.`{view}`"

    def _audit(self) -> str:
        return f"{_q(self.catalog)}.{_q(self.meta_schema)}.{_q(AUDIT_TABLE)}"

    # -- domains -----------------------------------------------------------------------------

    def _schema_tags(self) -> dict[str, dict[str, str]]:
        rows, _ = self._run(
            f"SELECT schema_name, tag_name, tag_value FROM {self._info('schema_tags')} WHERE catalog_name = :catalog",
            {"catalog": self.catalog},
        )
        out: dict[str, dict[str, str]] = {}
        for schema, k, v in rows:
            out.setdefault(schema, {})[k] = v
        return out

    def list_domains(self) -> list[DomainDef]:
        rows, _ = self._run(
            f"SELECT schema_name, comment, schema_owner FROM {self._info('schemata')} "
            "WHERE catalog_name = :catalog ORDER BY schema_name",
            {"catalog": self.catalog},
        )
        counts = dict(
            self._run(
                f"SELECT table_schema, count(*) FROM {self._info('tables')} "
                "WHERE table_catalog = :catalog AND table_type IN ('MANAGED', 'EXTERNAL') GROUP BY table_schema",
                {"catalog": self.catalog},
            )[0]
        )
        tags = self._schema_tags()
        domains = []
        for name, comment, owner in rows:
            if name in HIDDEN_SCHEMAS:
                continue
            t = tags.get(name, {})
            domains.append(
                DomainDef(
                    name=name,
                    display_name=t.get(TAG_DISPLAY_NAME, ""),
                    description=comment or "",
                    owner=t.get(TAG_OWNER, "") or (owner or ""),
                    form_count=int(counts.get(name, 0)),
                    properties={"schema_owner": owner or "", **t},
                )
            )
        return domains

    def get_domain(self, name: str) -> DomainDef:
        for d in self.list_domains():
            if d.name == name:
                return d
        raise NotFoundError(f"Domain '{name}' does not exist.")

    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        raise BackendError(
            "Domains are managed as infrastructure (Databricks Asset Bundle, resources/schemas.yml). "
            "Add the schema and its grants there and deploy."
        )

    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        self._run(f"COMMENT ON SCHEMA {self._s(domain.name)} IS {lit(domain.description or '')}")
        self._set_tags(
            f"ALTER SCHEMA {self._s(domain.name)}",
            {TAG_DISPLAY_NAME: domain.display_name, TAG_OWNER: domain.owner},
        )
        return self.get_domain(domain.name)

    def _set_tags(self, alter_prefix: str, tags: dict[str, str]) -> None:
        """Tags are a best-effort mirror: they need APPLY TAG, which not every admin has."""
        pairs = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in tags.items() if v)
        unset = [k for k, v in tags.items() if not v]
        try:
            if pairs:
                self._run(f"{alter_prefix} SET TAGS ({pairs})")
            if unset:
                self._run(f"{alter_prefix} UNSET TAGS ({', '.join(lit(k) for k in unset)})")
        except BackendError as exc:
            log.warning("Could not set tags (%s); continuing", exc)

    # -- forms -------------------------------------------------------------------------------

    def _table_tags(self, domain: str) -> dict[str, dict[str, str]]:
        rows, _ = self._run(
            f"SELECT table_name, tag_name, tag_value FROM {self._info('table_tags')} "
            "WHERE catalog_name = :catalog AND schema_name = :schema",
            {"catalog": self.catalog, "schema": domain},
        )
        out: dict[str, dict[str, str]] = {}
        for table, k, v in rows:
            out.setdefault(table, {})[k] = v
        return out

    def list_forms(self, domain: str) -> list[FormDef]:
        validate_identifier(domain, "domain name")
        rows, _ = self._run(
            f"SELECT table_name, comment, table_owner, created, last_altered, last_altered_by FROM {self._info('tables')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_type IN ('MANAGED', 'EXTERNAL') "
            "ORDER BY table_name",
            {"catalog": self.catalog, "schema": domain},
        )
        tags = self._table_tags(domain)
        forms = []
        for name, comment, owner, created, altered, altered_by in rows:
            t = tags.get(name, {})
            forms.append(
                FormDef(
                    domain=domain,
                    name=name,
                    display_name=t.get(TAG_DISPLAY_NAME, ""),
                    description=comment or "",
                    owner=t.get(TAG_OWNER, "") or (owner or ""),
                    tags=t,
                    created_at=to_db_scalar(created),
                    updated_at=to_db_scalar(altered),
                    updated_by=altered_by or "",
                )
            )
        return forms

    def _load_columns(self, domain: str, name: str) -> list[ColumnDef]:
        rows, _ = self._run(
            f"SELECT column_name, full_data_type, is_nullable, comment, ordinal_position FROM {self._info('columns')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_name = :table ORDER BY ordinal_position",
            {"catalog": self.catalog, "schema": domain, "table": name},
        )
        cols = []
        for i, (cname, dtype, nullable, comment, _pos) in enumerate(rows):
            t, p, s = parse_native_type(dtype)
            cols.append(
                ColumnDef(
                    name=cname,
                    data_type=t,
                    description=comment or "",
                    nullable=str(nullable).upper() != "NO",
                    precision=p,
                    scale=s,
                    native_type=str(dtype).upper() if dtype else "",
                    position=i,
                )
            )
        return cols

    def _properties(self, form: FormDef) -> dict[str, str]:
        try:
            rows, cols = self._run(f"SHOW TBLPROPERTIES {self._t(form)}")
        except BackendError:
            return {}
        return {str(r[0]): ("" if r[1] is None else str(r[1])) for r in rows}

    def get_form(self, domain: str, name: str) -> FormDef:
        validate_identifier(domain, "domain name")
        validate_identifier(name, "form name")
        rows, _ = self._run(
            f"SELECT comment, table_owner, created, last_altered, last_altered_by FROM {self._info('tables')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_name = :table",
            {"catalog": self.catalog, "schema": domain, "table": name},
        )
        if not rows:
            raise NotFoundError(f"Form '{domain}.{name}' does not exist.")
        comment, owner, created, altered, altered_by = rows[0]
        form = FormDef(
            domain=domain, name=name, description=comment or "", columns=self._load_columns(domain, name)
        )
        form.properties = self._properties(form)
        form.tags = self._table_tags(domain).get(name, {})
        form.display_name = form.properties.get(PROP_DISPLAY_NAME) or form.tags.get(TAG_DISPLAY_NAME, "")
        form.owner = form.properties.get(PROP_OWNER) or form.tags.get(TAG_OWNER, "") or (owner or "")
        form.apply_column_config(form.properties.get(PROP_COLUMN_CONFIG))
        form.created_at = to_db_scalar(created)
        form.updated_at = to_db_scalar(altered)
        form.updated_by = altered_by or ""
        form.row_count = self.count_rows(form)
        return form

    @staticmethod
    def _column_ddl(col: ColumnDef) -> str:
        ddl = f"{_q(col.name)} {native_type_databricks(col)}"
        if not col.nullable:
            ddl += " NOT NULL"
        if col.description:
            ddl += f" COMMENT {lit(col.description)}"
        return ddl

    def _rdm_properties(self, form: FormDef, actor: User | None = None) -> dict[str, str]:
        props = {
            PROP_FORM: "true",
            PROP_DISPLAY_NAME: form.display_name or "",
            PROP_OWNER: form.owner or (actor.username if actor else ""),
            PROP_COLUMN_CONFIG: form.column_config_json(),
        }
        return props

    def create_form(self, form: FormDef, actor: User, rows: pd.DataFrame | None = None) -> FormDef:
        user_cols = [c for c in form.columns if not c.is_system]
        form.columns = system_columns() + user_cols
        form.validate()
        props = {**FORM_TABLE_PROPERTIES, **self._rdm_properties(form, actor)}
        col_ddl = ",\n  ".join(self._column_ddl(c) for c in form.columns)
        prop_ddl = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items())
        self._run(
            f"CREATE TABLE {self._t(form)} (\n  {col_ddl},\n"
            f"  CONSTRAINT {_q('pk_' + form.name)} PRIMARY KEY ({_q(ID_COLUMN)})\n)\n"
            f"USING DELTA\nCOMMENT {lit(form.description or '')}\nTBLPROPERTIES ({prop_ddl})"
        )
        self._set_tags(
            f"ALTER TABLE {self._t(form)}",
            {TAG_FORM: "true", TAG_DISPLAY_NAME: form.display_name, TAG_OWNER: form.owner or actor.username},
        )
        if rows is not None and len(rows):
            self._append_rows(form, rows, actor)
        return self.get_form(form.domain, form.name)

    def update_form_metadata(self, form: FormDef, actor: User) -> FormDef:
        form.validate()
        current = {c.name: c for c in self._load_columns(form.domain, form.name)}
        for c in form.columns:
            if c.name not in current:
                raise NotFoundError(f"Column '{c.name}' does not exist on '{form.full_name}'.")
        self._run(f"COMMENT ON TABLE {self._t(form)} IS {lit(form.description or '')}")
        for c in form.columns:
            old = current[c.name]
            if (old.description or "") != (c.description or ""):
                self._run(
                    f"ALTER TABLE {self._t(form)} ALTER COLUMN {_q(c.name)} COMMENT {lit(c.description or '')}"
                )
            if not c.is_system and old.nullable != c.nullable:
                clause = "DROP NOT NULL" if c.nullable else "SET NOT NULL"
                try:
                    self._run(f"ALTER TABLE {self._t(form)} ALTER COLUMN {_q(c.name)} {clause}")
                except BackendError as exc:
                    raise BackendError(
                        f"Cannot make '{c.name}' required: {exc}. Fill in the empty values first."
                    ) from exc
        props = self._rdm_properties(form)
        prop_ddl = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items())
        self._run(f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({prop_ddl})")
        self._set_tags(
            f"ALTER TABLE {self._t(form)}", {TAG_DISPLAY_NAME: form.display_name, TAG_OWNER: form.owner}
        )
        return self.get_form(form.domain, form.name)

    def add_column(self, form: FormDef, column: ColumnDef, actor: User) -> FormDef:
        column.validate()
        if column.is_system:
            raise BackendError("System columns cannot be added manually.")
        if column.name in {c.name for c in self._load_columns(form.domain, form.name)}:
            raise ConflictError(f"Column '{column.name}' already exists.")
        column.nullable = True
        comment = f" COMMENT {lit(column.description)}" if column.description else ""
        self._run(
            f"ALTER TABLE {self._t(form)} ADD COLUMN {_q(column.name)} {native_type_databricks(column)}{comment}"
        )
        form.columns.append(column)
        self._run(
            f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({lit(PROP_COLUMN_CONFIG)} = {lit(form.column_config_json())})"
        )
        return self.get_form(form.domain, form.name)

    def drop_column(self, form: FormDef, column_name: str, actor: User) -> FormDef:
        if column_name in SYSTEM_COLUMNS:
            raise BackendError("System columns cannot be removed.")
        if column_name not in {c.name for c in self._load_columns(form.domain, form.name)}:
            raise NotFoundError(f"Column '{column_name}' does not exist.")
        self._run(f"ALTER TABLE {self._t(form)} DROP COLUMN {_q(column_name)}")
        form.columns = [c for c in form.columns if c.name != column_name]
        self._run(
            f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({lit(PROP_COLUMN_CONFIG)} = {lit(form.column_config_json())})"
        )
        return self.get_form(form.domain, form.name)

    def drop_form(self, form: FormDef, actor: User) -> None:
        self._run(f"DROP TABLE {self._t(form)}")

    # -- rows --------------------------------------------------------------------------------

    def _select_columns(self, form: FormDef) -> list[str]:
        return [c.name for c in form.columns]

    def read_rows(
        self,
        form: FormDef,
        search: str | None = None,
        limit: int = 5000,
        order_by: str | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        cols = self._select_columns(form)
        select = ", ".join(_q(c) for c in cols) if cols else "*"
        params: dict[str, Any] = {}
        where = ""
        if search:
            pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            search_cols = [c.name for c in form.user_columns] or cols
            clauses = [f"CAST({_q(c)} AS STRING) ILIKE :pattern ESCAPE '\\\\'" for c in search_cols]
            where = " WHERE " + " OR ".join(clauses)
            params["pattern"] = pattern
        direction = "DESC" if descending else "ASC"
        if order_by and order_by in cols:
            order = f" ORDER BY {_q(order_by)} {direction} NULLS LAST"
            if form.has_system_columns:
                order += f", {_q(ID_COLUMN)}"
        elif form.key_columns:
            keys = ", ".join(f"{_q(k.name)} {direction} NULLS LAST" for k in form.key_columns)
            order = f" ORDER BY {keys}, {_q(ID_COLUMN)}"
        elif form.has_system_columns:
            order = f" ORDER BY {_q(CREATED_AT_COLUMN)} {direction} NULLS LAST, {_q(ID_COLUMN)}"
        elif cols:
            order = f" ORDER BY {_q(cols[0])} {direction}"
        else:
            order = ""
        df = self._run_df(
            f"SELECT {select} FROM {self._t(form)}{where}{order} LIMIT {int(limit)}", params or None
        )
        return normalise_frame(df, form)

    def count_rows(self, form: FormDef) -> int:
        rows, _ = self._run(f"SELECT count(*) FROM {self._t(form)}")
        return int(rows[0][0]) if rows else 0

    # -- JSON payload helpers ----------------------------------------------------------------

    @staticmethod
    def _struct_schema(columns: Iterable[ColumnDef], extra: dict[str, str] | None = None) -> str:
        fields = [f"{k}:{v}" for k, v in (extra or {}).items()]
        for c in columns:
            fields.append(f"{c.name}:{native_type_databricks(c)}")
        return "ARRAY<STRUCT<" + ",".join(fields) + ">>"

    @staticmethod
    def _from_json(schema: str, param: str = "payload") -> str:
        return (
            f"SELECT inline(from_json(:{param}, {lit(schema)}, "
            f"map('timestampNTZFormat', {lit(TS_FORMAT)}, 'timestampFormat', {lit(TS_FORMAT)}, 'dateFormat', 'yyyy-MM-dd')))"
        )

    def _fetch_rows(self, form: FormDef, ids: list[str]) -> dict[str, dict[str, Any]]:
        """Current values of the given rows (chunked to respect the 255 parameter markers limit)."""
        cols = self._select_columns(form)
        out: dict[str, dict[str, Any]] = {}
        for i in range(0, len(ids), 200):
            chunk = ids[i : i + 200]
            params = {f"p{j}": rid for j, rid in enumerate(chunk)}
            markers = ", ".join(f":{k}" for k in params)
            rows, names = self._run(
                f"SELECT {', '.join(_q(c) for c in cols)} FROM {self._t(form)} WHERE {_q(ID_COLUMN)} IN ({markers})",
                params,
            )
            for r in rows:
                rec = dict(zip(names, r, strict=True))
                out[str(rec[ID_COLUMN])] = rec
        return out

    def apply_changes(self, form: FormDef, changes: ChangeSet, actor: User) -> SaveResult:
        if not form.is_editable:
            raise BackendError("This table has no system columns and cannot be edited row by row.")
        result = SaveResult()
        now = utcnow()
        batch = uuid.uuid4().hex
        editable = [c for c in form.user_columns if c.data_type is not DataType.OTHER]
        wanted = [u.row_id for u in changes.updates] + [d.row_id for d in changes.deletes]
        current = self._fetch_rows(form, wanted) if wanted else {}

        ops: list[dict[str, Any]] = []
        audit: list[dict[str, Any]] = []
        expected_updates = expected_deletes = 0
        for ins in changes.inserts:
            row_id = new_row_id()
            values = {c.name: _json_value(c, ins.values.get(c.name)) for c in editable}
            ops.append({"_op": "I", ID_COLUMN: row_id, VERSION_COLUMN: 1, **values})
            audit.append(
                self._audit_row(
                    form, row_id, "insert", actor, batch, None, {ID_COLUMN: row_id, **values}, now, len(audit)
                )
            )
        for upd in changes.updates:
            before = current.get(upd.row_id)
            if before is None or (
                upd.expected_version is not None and int(before[VERSION_COLUMN]) != int(upd.expected_version)
            ):
                result.conflicts.append(self._conflict_message(upd.label or upd.row_id, before))
                continue
            merged = {
                c.name: _json_value(c, upd.changes[c.name] if c.name in upd.changes else before.get(c.name))
                for c in editable
            }
            ops.append(
                {"_op": "U", ID_COLUMN: upd.row_id, VERSION_COLUMN: int(before[VERSION_COLUMN]), **merged}
            )
            after = {ID_COLUMN: upd.row_id, **merged}
            audit.append(
                self._audit_row(
                    form,
                    upd.row_id,
                    "update",
                    actor,
                    batch,
                    self._plain(before, editable),
                    after,
                    now,
                    len(audit),
                )
            )
            expected_updates += 1
        for dele in changes.deletes:
            before = current.get(dele.row_id)
            if before is None or (
                dele.expected_version is not None
                and int(before[VERSION_COLUMN]) != int(dele.expected_version)
            ):
                result.conflicts.append(self._conflict_message(dele.label or dele.row_id, before))
                continue
            ops.append({"_op": "D", ID_COLUMN: dele.row_id, VERSION_COLUMN: int(before[VERSION_COLUMN])})
            audit.append(
                self._audit_row(
                    form,
                    dele.row_id,
                    "delete",
                    actor,
                    batch,
                    self._plain(before, editable),
                    None,
                    now,
                    len(audit),
                )
            )
            expected_deletes += 1
        if not ops:
            return result

        schema = self._struct_schema(
            editable, {"_op": "STRING", ID_COLUMN: "STRING", VERSION_COLUMN: "BIGINT"}
        )
        set_clause = ", ".join(f"t.{_q(c.name)} = s.{_q(c.name)}" for c in editable)
        insert_cols = [
            ID_COLUMN,
            VERSION_COLUMN,
            CREATED_AT_COLUMN,
            CREATED_BY_COLUMN,
            UPDATED_AT_COLUMN,
            UPDATED_BY_COLUMN,
        ] + [c.name for c in editable]
        insert_vals = [f"s.{_q(ID_COLUMN)}", "1", ":now", ":actor", ":now", ":actor"] + [
            f"s.{_q(c.name)}" for c in editable
        ]
        merge = (
            f"MERGE INTO {self._t(form)} AS t\n"
            f"USING ({self._from_json(schema)}) AS s\n"
            f"ON t.{_q(ID_COLUMN)} = s.{_q(ID_COLUMN)}\n"
            f"WHEN MATCHED AND s.`_op` = 'D' AND t.{_q(VERSION_COLUMN)} = s.{_q(VERSION_COLUMN)} THEN DELETE\n"
            f"WHEN MATCHED AND s.`_op` = 'U' AND t.{_q(VERSION_COLUMN)} = s.{_q(VERSION_COLUMN)} THEN UPDATE SET "
            f"{set_clause}, t.{_q(VERSION_COLUMN)} = t.{_q(VERSION_COLUMN)} + 1, "
            f"t.{_q(UPDATED_AT_COLUMN)} = :now, t.{_q(UPDATED_BY_COLUMN)} = :actor\n"
            f"WHEN NOT MATCHED AND s.`_op` = 'I' THEN INSERT ({', '.join(_q(c) for c in insert_cols)}) "
            f"VALUES ({', '.join(insert_vals)})"
        )
        for i in range(0, len(ops), JSON_ROWS_CHUNK):
            chunk = ops[i : i + JSON_ROWS_CHUNK]
            rows, names = self._run(
                merge, {"payload": json.dumps(chunk), "now": now, "actor": actor.username}
            )
            metrics = dict(zip(names, rows[0], strict=False)) if rows else {}
            result.inserted += int(
                metrics.get("num_inserted_rows", sum(1 for o in chunk if o["_op"] == "I")) or 0
            )
            result.updated += int(
                metrics.get("num_updated_rows", sum(1 for o in chunk if o["_op"] == "U")) or 0
            )
            result.deleted += int(
                metrics.get("num_deleted_rows", sum(1 for o in chunk if o["_op"] == "D")) or 0
            )
        if result.updated < expected_updates or result.deleted < expected_deletes:
            # Someone changed a row between our read and the MERGE: find out which.
            after = self._fetch_rows(form, [o[ID_COLUMN] for o in ops if o["_op"] in ("U", "D")])
            for o in ops:
                if (
                    o["_op"] == "U"
                    and int(after.get(o[ID_COLUMN], {}).get(VERSION_COLUMN, -1)) != o[VERSION_COLUMN] + 1
                ):
                    result.conflicts.append(self._conflict_message(o[ID_COLUMN], after.get(o[ID_COLUMN])))
                if o["_op"] == "D" and o[ID_COLUMN] in after:
                    result.conflicts.append(self._conflict_message(o[ID_COLUMN], after.get(o[ID_COLUMN])))
        self._write_audit(audit)
        return result

    @staticmethod
    def _plain(row: dict[str, Any], columns: list[ColumnDef]) -> dict[str, Any]:
        out = {ID_COLUMN: row.get(ID_COLUMN)}
        for c in columns:
            out[c.name] = _json_value(c, row.get(c.name))
        return out

    @staticmethod
    def _conflict_message(label: str, current: dict[str, Any] | None) -> str:
        if current is None:
            return f"{label}: the row was deleted by someone else."
        who = current.get(UPDATED_BY_COLUMN) or "someone else"
        when = to_db_scalar(current.get(UPDATED_AT_COLUMN))
        when_txt = f" at {when:%Y-%m-%d %H:%M:%S} UTC" if isinstance(when, datetime) else ""
        return f"{label}: modified by {who}{when_txt} after you loaded it."

    def append_rows(self, form: FormDef, rows: pd.DataFrame, actor: User) -> int:
        if not form.is_editable:
            raise BackendError("This table has no system columns and cannot be edited.")
        return self._append_rows(form, rows, actor)

    def _append_rows(self, form: FormDef, rows: pd.DataFrame, actor: User) -> int:
        if rows is None or rows.empty:
            return 0
        now = utcnow()
        batch = uuid.uuid4().hex
        editable = [c for c in form.user_columns if c.data_type is not DataType.OTHER]
        schema = self._struct_schema(editable, {ID_COLUMN: "STRING"})
        cols = [
            ID_COLUMN,
            VERSION_COLUMN,
            CREATED_AT_COLUMN,
            CREATED_BY_COLUMN,
            UPDATED_AT_COLUMN,
            UPDATED_BY_COLUMN,
        ] + [c.name for c in editable]
        select = [f"s.{_q(ID_COLUMN)}", "1", ":now", ":actor", ":now", ":actor"] + [
            f"s.{_q(c.name)}" for c in editable
        ]
        statement = (
            f"INSERT INTO {self._t(form)} ({', '.join(_q(c) for c in cols)})\n"
            f"SELECT {', '.join(select)} FROM ({self._from_json(schema)}) AS s"
        )
        records = rows.to_dict("records")
        total = 0
        audit: list[dict[str, Any]] = []
        for i in range(0, len(records), JSON_ROWS_CHUNK):
            payload = []
            for rec in records[i : i + JSON_ROWS_CHUNK]:
                row_id = new_row_id()
                values = {c.name: _json_value(c, rec.get(c.name)) for c in editable}
                payload.append({ID_COLUMN: row_id, **values})
                audit.append(
                    self._audit_row(
                        form,
                        row_id,
                        "insert",
                        actor,
                        batch,
                        None,
                        {ID_COLUMN: row_id, **values},
                        now,
                        len(audit),
                    )
                )
            self._run(statement, {"payload": json.dumps(payload), "now": now, "actor": actor.username})
            total += len(payload)
        self._write_audit(audit)
        return total

    # -- audit log ---------------------------------------------------------------------------

    @staticmethod
    def _audit_row(
        form: FormDef,
        row_id: str,
        change_type: str,
        actor: User,
        batch: str,
        before: dict | None,
        after: dict | None,
        when: datetime,
        seq: int,
    ) -> dict[str, Any]:
        return {
            "id": uuid.uuid4().hex,
            "seq": seq,
            "schema_name": form.domain,
            "table_name": form.name,
            "row_id": row_id,
            "change_type": change_type,
            "changed_at": when.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "changed_by": actor.username,
            "batch_id": batch,
            "before_json": json.dumps(before) if before is not None else None,
            "after_json": json.dumps(after) if after is not None else None,
        }

    AUDIT_SCHEMA = (
        "ARRAY<STRUCT<id:STRING,seq:BIGINT,schema_name:STRING,table_name:STRING,row_id:STRING,change_type:STRING,"
        "changed_at:TIMESTAMP_NTZ,changed_by:STRING,batch_id:STRING,before_json:STRING,after_json:STRING>>"
    )

    def audit_table_ddl(self) -> str:
        """DDL for the audit table (also created by the bundle's setup job)."""
        return (
            f"CREATE TABLE IF NOT EXISTS {self._audit()} (\n"
            "  id STRING NOT NULL, seq BIGINT, schema_name STRING NOT NULL, table_name STRING NOT NULL, row_id STRING,\n"
            "  change_type STRING NOT NULL, changed_at TIMESTAMP_NTZ NOT NULL, changed_by STRING, batch_id STRING,\n"
            "  before_json STRING, after_json STRING\n"
            ") USING DELTA COMMENT 'Row-level change history written by the Reference Data Manager app' "
            "TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')"
        )

    def _write_audit(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        statement = (
            f"INSERT INTO {self._audit()} (id, seq, schema_name, table_name, row_id, change_type, changed_at, changed_by, "
            f"batch_id, before_json, after_json)\nSELECT id, seq, schema_name, table_name, row_id, change_type, changed_at, "
            f"changed_by, batch_id, before_json, after_json FROM ({self._from_json(self.AUDIT_SCHEMA)}) AS s"
        )
        for i in range(0, len(rows), JSON_ROWS_CHUNK):
            payload = json.dumps(rows[i : i + JSON_ROWS_CHUNK])
            try:
                self._run(statement, {"payload": payload})
                self._audit_ready = True
            except NotFoundError:
                try:
                    self._run(self.audit_table_ddl())
                    self._run(statement, {"payload": payload})
                    self._audit_ready = True
                except BackendError as exc:
                    self._audit_ready = False
                    log.warning("Audit log unavailable (%s); history falls back to Change Data Feed", exc)
                    return
            except BackendError as exc:
                log.warning("Audit log write failed: %s", exc)
                return

    def get_history(self, form: FormDef, limit: int = 200) -> pd.DataFrame:
        user_cols = [c.name for c in form.user_columns]
        columns = [*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, *user_cols]
        if self._audit_ready is not False:
            try:
                rows, _ = self._run(
                    f"SELECT changed_at, changed_by, change_type, row_id, before_json, after_json FROM {self._audit()} "
                    "WHERE schema_name = :schema AND table_name = :table ORDER BY changed_at DESC, seq DESC LIMIT "
                    + str(int(limit)),
                    {"schema": form.domain, "table": form.name},
                )
                self._audit_ready = True
                records = []
                n = len(rows)
                for i, (changed_at, changed_by, change_type, row_id, before_json, after_json) in enumerate(
                    rows
                ):
                    before = json.loads(before_json) if before_json else {}
                    after = json.loads(after_json) if after_json else {}
                    snapshot = after if change_type != "delete" else before
                    changed = [
                        c for c in user_cols if change_type == "update" and before.get(c) != after.get(c)
                    ]
                    rec = {
                        "version": n - i,
                        "changed_at": to_db_scalar(changed_at),
                        "changed_by": changed_by,
                        "change_type": change_type,
                        "changed_fields": ", ".join(changed),
                        ID_COLUMN: row_id,
                    }
                    for c in user_cols:
                        rec[c] = snapshot.get(c)
                    records.append(rec)
                return pd.DataFrame.from_records(records, columns=columns)
            except NotFoundError:
                self._audit_ready = False
        return self._history_from_cdf(form, limit, columns)

    def _history_from_cdf(self, form: FormDef, limit: int, columns: list[str]) -> pd.DataFrame:
        """Fallback: Delta Change Data Feed (bounded by the table's retention settings)."""
        try:
            hist, _ = self._run(f"DESCRIBE HISTORY {self._t(form)}")
            versions = [int(r[0]) for r in hist] if hist else [0]
            start = max(0, min(versions))
            df = self._run_df(
                f"SELECT _change_type, _commit_version, _commit_timestamp, {', '.join(_q(c) for c in [ID_COLUMN, UPDATED_BY_COLUMN, *user_cols])} "
                f"FROM table_changes({lit(f'{self.catalog}.{form.domain}.{form.name}')}, {start}) "
                "WHERE _change_type <> 'update_preimage' ORDER BY _commit_version DESC LIMIT "
                + str(int(limit))
                if (user_cols := [c.name for c in form.user_columns]) is not None
                else ""
            )
        except BackendError as exc:
            log.warning("Change Data Feed history unavailable: %s", exc)
            return pd.DataFrame(columns=columns)
        if df.empty:
            return pd.DataFrame(columns=columns)
        kinds = {"insert": "insert", "update_postimage": "update", "delete": "delete"}
        out = pd.DataFrame(
            {
                "version": df["_commit_version"],
                "changed_at": pd.to_datetime(df["_commit_timestamp"]),
                "changed_by": df[UPDATED_BY_COLUMN],
                "change_type": df["_change_type"].map(kinds).fillna(df["_change_type"]),
                "changed_fields": "",
                ID_COLUMN: df[ID_COLUMN],
            }
        )
        for c in user_cols:
            out[c] = df[c]
        return out[columns]

    # -- authorisation -------------------------------------------------------------------

    def _principal_filter(self, user: User) -> tuple[str, dict[str, Any]]:
        """SQL predicate matching the user's principals.

        With user authorization the statement runs as the user, so Unity Catalog resolves
        (nested) group membership for us. In service-principal mode we fall back to the
        principals resolved by the auth provider.
        """
        if self.access_token:
            return "(grantee = current_user() OR is_account_group_member(grantee))", {}
        principals = sorted(user.principals)
        params = {f"g{i}": p for i, p in enumerate(principals)}
        return "grantee IN (" + ", ".join(f":{k}" for k in params) + ")", params

    def get_permissions(self, user: User) -> Permissions:
        predicate, params = self._principal_filter(user)
        roles: dict[str, Role] = {}
        rows, _ = self._run(
            f"SELECT schema_name FROM {self._info('schemata')} WHERE catalog_name = :catalog",
            {"catalog": self.catalog},
        )
        schemas = [r[0] for r in rows if r[0] not in HIDDEN_SCHEMAS]
        for s in schemas:
            roles[s] = Role.NONE
        # Direct and inherited (catalog-level) schema privileges
        rows, _ = self._run(
            f"SELECT schema_name, privilege_type FROM {self._info('schema_privileges')} "
            f"WHERE catalog_name = :catalog AND {predicate}",
            {"catalog": self.catalog, **params},
        )
        privs: dict[str, set[str]] = {}
        for schema, priv in rows:
            privs.setdefault(schema, set()).add(str(priv).upper())
        # Catalog-level privileges apply to every schema
        rows, _ = self._run(
            f"SELECT privilege_type FROM {self._info('catalog_privileges')} WHERE catalog_name = :catalog AND {predicate}",
            {"catalog": self.catalog, **params},
        )
        catalog_privs = {str(r[0]).upper() for r in rows}
        # Owners hold every privilege implicitly
        owner_pred = predicate.replace("grantee", "schema_owner")
        rows, _ = self._run(
            f"SELECT schema_name FROM {self._info('schemata')} WHERE catalog_name = :catalog AND {owner_pred}",
            {"catalog": self.catalog, **params},
        )
        owned = {r[0] for r in rows}
        for s in schemas:
            p = privs.get(s, set()) | catalog_privs
            if s in owned:
                p = p | {"MANAGE"}
            roles[s] = self._role_from_privileges(p)
        return Permissions(domain_roles=roles, can_create_domain=False)

    @staticmethod
    def _role_from_privileges(privs: set[str]) -> Role:
        p = {x.replace("_", " ") for x in privs}
        if p & {x.replace("_", " ") for x in ADMIN_PRIVILEGES}:
            return Role.ADMIN
        if p & {x.replace("_", " ") for x in CREATE_PRIVILEGES} and p & EDIT_PRIVILEGES:
            return Role.ADMIN
        if p & EDIT_PRIVILEGES:
            return Role.EDITOR
        if p & VIEW_PRIVILEGES:
            return Role.VIEWER
        return Role.NONE

    def list_domain_grants(self, domain: str) -> list[tuple[str, Role]]:
        validate_identifier(domain, "domain name")
        rows, _ = self._run(
            f"SELECT grantee, privilege_type FROM {self._info('schema_privileges')} "
            "WHERE catalog_name = :catalog AND schema_name = :schema",
            {"catalog": self.catalog, "schema": domain},
        )
        by_grantee: dict[str, set[str]] = {}
        for grantee, priv in rows:
            by_grantee.setdefault(str(grantee), set()).add(str(priv).upper())
        return sorted((g, self._role_from_privileges(p)) for g, p in by_grantee.items())

    def grant_domain_role(self, domain: str, principal: str, role: Role, actor: User) -> None:
        raise BackendError(
            "Access is managed as infrastructure (Databricks Asset Bundle grants on the schema). "
            "Change resources/schemas.yml and deploy."
        )
