"""Databricks SQL warehouse implementation of :class:`DatabaseBackend`.

Production mapping (see docs/DESIGN.md):

* function = Unity Catalog schema, form = Delta table in ``<catalog>``. A *domain* (the
  business classifier above functions) is a registry entry in ``<catalog>._catalog.domains``;
  the function's domain is recorded as the schema property ``rdm.domain`` and the schema tag
  ``rdm_domain`` so that it is visible and searchable in Catalog Explorer.
* file = CSV/Parquet file in the managed volume ``<catalog>.<function>._files`` (created on
  first use). Files are moved with the Files API (SDK, as the user under user authorization
  with the ``files.files`` scope) and read with ``read_files`` on the warehouse; their
  descriptive attributes live in ``<catalog>._catalog.files``.
* Descriptions are ``COMMENT``s; display name / owner / column configuration live in
  ``TBLPROPERTIES ('rdm.*')`` and are mirrored to tags (``rdm_display_name``, ``rdm_owner``)
  so they are searchable in Catalog Explorer and readable in bulk from
  ``information_schema.table_tags``.
* A save is **one** ``MERGE`` statement whose source is a single JSON parameter
  (``inline(from_json(:payload, '<struct schema>'))``): atomic, idempotent on retry
  (app-generated ``_id``), no 255-parameter-marker limit. Concurrency uses the integer
  ``_version`` column.
* Row history is written to an explicit audit table ``<catalog>._catalog.change_log``; the
  same schema holds the ``domains``, ``functions`` and ``forms`` registry tables (descriptive
  attributes such as owner and documentation link, for reporting and lineage)
  (same shape as the DuckDB backend); Delta Change Data Feed is enabled on every form for
  downstream SCD pipelines and used as a fallback when the audit table is unavailable.
* Roles are derived from Unity Catalog privileges inside the SQL session
  (``current_user()`` / ``is_account_group_member``), so with user authorization enabled
  the app renders exactly what the warehouse will allow.
* Functions (schemas) can be created and dropped by global administrators (``CREATE SCHEMA``
  on the catalog) and access is granted to Unity Catalog *groups* only, through
  ``GRANT``/``REVOKE`` on the schema. The asset bundle seeds the initial functions and grants.

All values are bound as parameters; identifiers are validated and back-quoted; DDL clauses
that cannot take parameters (comments, properties, tags) are escaped with Spark rules.
"""

from __future__ import annotations

import io
import json
import logging
import re
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
    FILE_CHANGE_TYPES,
    FILE_FORMATS,
    FILES_VOLUME,
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
    SCD2_END_COLUMN,
    SCD2_START_COLUMN,
    SYSTEM_COLUMNS,
    TAG_DISPLAY_NAME,
    TAG_DOMAIN,
    TAG_FORM,
    TAG_OWNER,
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
    volume_file_path,
)

log = logging.getLogger(__name__)

META_SCHEMA = "_catalog"
AUDIT_TABLE = "change_log"
HIDDEN_SCHEMAS = frozenset({"information_schema", "default", META_SCHEMA})

#: Confinement guard (defence in depth, see docs/DESIGN.md "Security review"): every statement
#: the backend sends to the warehouse must stay inside ``self.catalog`` and must not carry a
#: clause that could delete more than one object at a time.
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'")
_THREE_PART_RE = re.compile(r"`([^`]+)`\.`([^`]+)`\.`([^`]+)`")
_ON_CATALOG_RE = re.compile(r"\bON\s+CATALOG\s+`([^`]+)`", re.IGNORECASE)
_TABLE_CHANGES_RE = re.compile(r"table_changes\(\s*'([^'.]+)\.", re.IGNORECASE)
_VOLUME_PATH_RE = re.compile(r"'/Volumes/([^/']+)/")
_FORBIDDEN_RE = re.compile(
    r"\bCASCADE\b|\bDROP\s+CATALOG\b|\bTRUNCATE\b|\bPURGE\b|\bVACUUM\b|\bUSE\s+CATALOG\b(?!\s+ON)",
    re.IGNORECASE,
)

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
GLOBAL_ADMIN_PRIVILEGES = {"CREATE SCHEMA", "CREATE_SCHEMA", "MANAGE", "ALL PRIVILEGES", "ALL_PRIVILEGES"}
#: Schema privileges the app manages per role (everything else is left untouched).
#: READ/WRITE VOLUME on the schema are inherited by the ``_files`` volume.
ROLE_PRIVILEGES = {
    Role.VIEWER: ["USE SCHEMA", "SELECT", "READ VOLUME"],
    Role.EDITOR: ["USE SCHEMA", "SELECT", "READ VOLUME", "MODIFY", "WRITE VOLUME"],
    Role.ADMIN: [
        "USE SCHEMA",
        "SELECT",
        "READ VOLUME",
        "MODIFY",
        "WRITE VOLUME",
        "CREATE TABLE",
        "CREATE VOLUME",
        "MANAGE",
        "APPLY TAG",
    ],
}
MANAGED_PRIVILEGES = ROLE_PRIVILEGES[Role.ADMIN]
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
        files_client_factory: Callable[[], Any] | None = None,
    ) -> None:
        validate_identifier(catalog, "catalog name")
        validate_identifier(meta_schema, "schema name")
        self.catalog = catalog
        self.http_path = http_path
        self.host = host
        self.access_token = access_token
        self.meta_schema = meta_schema
        self._connection_factory = connection_factory or self._connect
        self._files_client_factory = files_client_factory or self._files_client
        self._conn: Any = None
        self._files: Any = None
        self._lock = threading.RLock()
        self._audit_ready: bool | None = None
        self._volumes_ready: set[str] = set()

    # -- connection --------------------------------------------------------------------------

    def describe(self) -> str:
        mode = "as you" if self.access_token else "as app service principal"
        return f"Databricks SQL ({self.http_path.rsplit('/', 1)[-1]}) · catalog {self.catalog} · {mode}"

    def _connect(self) -> Any:
        from databricks import sql
        from databricks.sdk.core import Config

        cfg = Config(host=self.host) if self.host else Config()
        hostname = (cfg.host or "").replace("https://", "").replace("http://", "").rstrip("/")
        # Naive timestamps are UTC by contract; without this the warehouse session zone
        # (workspace-local) would reinterpret them on write while reads return UTC.
        session_conf = {"timezone": "UTC"}
        if self.access_token:
            return sql.connect(
                server_hostname=hostname,
                http_path=self.http_path,
                access_token=self.access_token,
                session_configuration=session_conf,
            )
        return sql.connect(
            server_hostname=hostname,
            http_path=self.http_path,
            credentials_provider=lambda: cfg.authenticate,
            session_configuration=session_conf,
        )

    def _connection(self) -> Any:
        if self._conn is None:
            self._conn = self._connection_factory()
        return self._conn

    def _files_client(self) -> Any:
        """The SDK Files API: as the user under user authorization, else as the app identity."""
        from databricks.sdk import WorkspaceClient

        if self.access_token:
            return WorkspaceClient(host=self.host, token=self.access_token, auth_type="pat").files
        return WorkspaceClient(host=self.host).files if self.host else WorkspaceClient().files

    def _files_api(self) -> Any:
        if self._files is None:
            self._files = self._files_client_factory()
        return self._files

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
        self._assert_confined(statement)
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
        self._assert_confined(statement)
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

    # -- confinement ---------------------------------------------------------------------------

    def _assert_confined(self, statement: str) -> None:
        """Refuse any statement that reaches outside ``self.catalog`` or could cascade a deletion.

        The SQL is built by this module from validated identifiers, so this never triggers in
        normal operation; it is a last line of defence against a future coding mistake.
        """
        code = _STRING_LITERAL_RE.sub("''", statement)
        forbidden = _FORBIDDEN_RE.search(code)
        if forbidden:
            raise BackendError(f"Refusing statement with '{forbidden.group(0)}': not allowed by the app.")
        foreign = {m.group(1) for m in _THREE_PART_RE.finditer(code)} - {self.catalog}
        foreign |= {m.group(1) for m in _ON_CATALOG_RE.finditer(code)} - {self.catalog}
        foreign |= {m.group(1) for m in _TABLE_CHANGES_RE.finditer(statement)} - {self.catalog}
        foreign |= {m.group(1) for m in _VOLUME_PATH_RE.finditer(statement)} - {self.catalog}
        if foreign:
            raise BackendError(
                f"Refusing statement outside catalog '{self.catalog}': {', '.join(sorted(foreign))}."
            )

    def _assert_volume_path(self, path: str) -> str:
        """Only ``/Volumes/<catalog>/<function>/_files[/<file>]`` may be listed, read, written or deleted."""
        parts = path.split("/")
        if (
            len(parts) not in (5, 6)
            or parts[:2] != ["", "Volumes"]
            or parts[2] != self.catalog
            or parts[4] != FILES_VOLUME
            or not all(parts[2:])
        ):
            raise BackendError(f"Refusing to touch a path outside the '{self.catalog}' file volumes: {path}")
        validate_identifier(parts[3], "function name", allow_leading_underscore=False)
        if len(parts) == 6:
            validate_file_name(parts[5])
        return path

    # -- naming ------------------------------------------------------------------------------

    def _t(self, form: FormDef) -> str:
        return f"{_q(self.catalog)}.{_q(form.function)}.{_q(form.name)}"

    def _s(self, function: str) -> str:
        return f"{_q(self.catalog)}.{_q(function)}"

    def _info(self, view: str) -> str:
        return f"{_q(self.catalog)}.`information_schema`.`{view}`"

    def _audit(self) -> str:
        return f"{_q(self.catalog)}.{_q(self.meta_schema)}.{_q(AUDIT_TABLE)}"

    # -- domains (classifier, registry only) ----------------------------------------------

    def _domain_rows(self) -> list[tuple]:
        try:
            rows, _ = self._run(
                f"SELECT name, display_name, description, owner FROM {self._reg('domains')} ORDER BY name"
            )
        except NotFoundError:
            return []
        return rows

    def _function_counts_by_domain(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for f in self.list_functions():
            if f.domain:
                counts[f.domain] = counts.get(f.domain, 0) + 1
        return counts

    def list_domains(self) -> list[DomainDef]:
        counts = self._function_counts_by_domain()
        return [
            DomainDef(
                name=name,
                display_name=display or "",
                description=description or "",
                owner=owner or "",
                function_count=counts.get(name, 0),
            )
            for name, display, description, owner in self._domain_rows()
        ]

    def get_domain(self, name: str) -> DomainDef:
        validate_identifier(name, "domain name")
        for d in self.list_domains():
            if d.name == name:
                return d
        raise NotFoundError(f"Domain '{name}' does not exist.")

    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        if any(r[0] == domain.name for r in self._domain_rows()):
            raise ConflictError(f"Domain '{domain.name}' already exists.")
        self._register_domain(domain, actor, strict=True)
        return self.get_domain(domain.name)

    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        domain.validate()
        if not any(r[0] == domain.name for r in self._domain_rows()):
            raise NotFoundError(f"Domain '{domain.name}' does not exist.")
        self._register_domain(domain, actor, strict=True)
        return self.get_domain(domain.name)

    def delete_domain(self, name: str, actor: User) -> None:
        validate_identifier(name, "domain name")
        if not any(r[0] == name for r in self._domain_rows()):
            raise NotFoundError(f"Domain '{name}' does not exist.")
        assigned = self._function_counts_by_domain().get(name, 0)
        if assigned:
            raise ConflictError(
                f"Domain '{name}' still has {assigned} function(s) assigned. Move them to another domain first."
            )
        self._run(f"DELETE FROM {self._reg('domains')} WHERE `name` = :name", {"name": name})

    # -- functions (schemas) ------------------------------------------------------------------

    def _schema_tags(self) -> dict[str, dict[str, str]]:
        rows, _ = self._run(
            f"SELECT schema_name, tag_name, tag_value FROM {self._info('schema_tags')} WHERE catalog_name = :catalog",
            {"catalog": self.catalog},
        )
        out: dict[str, dict[str, str]] = {}
        for schema, k, v in rows:
            out.setdefault(schema, {})[k] = v
        return out

    def list_functions(self) -> list[FunctionDef]:
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
        registry = self._function_registry()
        file_counts = self._file_counts()
        functions = []
        for name, comment, owner in rows:
            if name in HIDDEN_SCHEMAS:
                continue
            t = tags.get(name, {})
            r = registry.get(name, {})
            functions.append(
                FunctionDef(
                    name=name,
                    display_name=r.get("display_name") or t.get(TAG_DISPLAY_NAME, ""),
                    description=comment or r.get("description") or "",
                    owner=r.get("owner") or t.get(TAG_OWNER, "") or (owner or ""),
                    owner_email=r.get("owner_email") or "",
                    doc_link=r.get("doc_link") or "",
                    domain=r.get("domain_name") or t.get(TAG_DOMAIN, "") or "",
                    form_count=int(counts.get(name, 0)),
                    file_count=int(file_counts.get(name, 0)),
                    properties={"schema_owner": owner or "", **t},
                )
            )
        return functions

    def _function_registry(self) -> dict[str, dict[str, Any]]:
        """Descriptive attributes from ``_catalog.functions`` (empty when the table is absent).

        ``SELECT *`` keeps the read tolerant of registries created before newer columns.
        """
        try:
            rows, cols = self._run(f"SELECT * FROM {self._reg('functions')}")
        except BackendError:
            return {}
        out = {}
        for r in rows:
            d = dict(zip(cols, r, strict=True))
            out[d["name"]] = d
        return out

    def _reg(self, table: str) -> str:
        return f"{_q(self.catalog)}.{_q(self.meta_schema)}.{_q(table)}"

    def get_function(self, name: str) -> FunctionDef:
        for f in self.list_functions():
            if f.name == name:
                return f
        raise NotFoundError(f"Function '{name}' does not exist.")

    def _check_domain(self, domain: str) -> None:
        if domain and not any(r[0] == domain for r in self._domain_rows()):
            raise NotFoundError(f"Domain '{domain}' does not exist.")

    def create_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        """``CREATE SCHEMA`` in the catalog: requires CREATE SCHEMA on the catalog (global admin)."""
        function.validate()
        if function.name in HIDDEN_SCHEMAS:
            raise ConflictError(f"'{function.name}' is a reserved name.")
        self._check_domain(function.domain)
        props = {
            PROP_DISPLAY_NAME: function.display_name,
            PROP_OWNER: function.owner or actor.username,
            PROP_OWNER_EMAIL: function.owner_email,
            PROP_DOC_LINK: function.doc_link,
            PROP_DOMAIN: function.domain,
            "rdm.created_by": actor.username,
        }
        prop_ddl = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items() if v)
        self._run(
            f"CREATE SCHEMA {self._s(function.name)} COMMENT {lit(function.description or '')}"
            + (f" WITH DBPROPERTIES ({prop_ddl})" if prop_ddl else "")
        )
        self._set_tags(
            f"ALTER SCHEMA {self._s(function.name)}",
            {
                TAG_DISPLAY_NAME: function.display_name,
                TAG_OWNER: function.owner or actor.username,
                TAG_DOMAIN: function.domain,
            },
        )
        self._register_function(function, actor)
        return self.get_function(function.name)

    def update_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        function.validate()
        self._check_domain(function.domain)
        self._run(f"COMMENT ON SCHEMA {self._s(function.name)} IS {lit(function.description or '')}")
        props = {
            PROP_DISPLAY_NAME: function.display_name,
            PROP_OWNER: function.owner,
            PROP_OWNER_EMAIL: function.owner_email,
            PROP_DOC_LINK: function.doc_link,
            PROP_DOMAIN: function.domain,
        }
        prop_ddl = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items())
        self._run(f"ALTER SCHEMA {self._s(function.name)} SET DBPROPERTIES ({prop_ddl})")
        self._set_tags(
            f"ALTER SCHEMA {self._s(function.name)}",
            {TAG_DISPLAY_NAME: function.display_name, TAG_OWNER: function.owner, TAG_DOMAIN: function.domain},
        )
        self._register_function(function, actor)
        return self.get_function(function.name)

    def drop_function(self, function: FunctionDef, actor: User) -> None:
        """``DROP SCHEMA ... RESTRICT``: refused while the schema still holds tables or its volume
        holds anything (or cannot be listed). Never cascades."""
        validate_identifier(function.name, "function name")
        rows, _ = self._run(
            f"SELECT count(*) FROM {self._info('tables')} WHERE table_catalog = :catalog AND table_schema = :schema",
            {"catalog": self.catalog, "schema": function.name},
        )
        n = int(rows[0][0]) if rows else 0
        if n:
            raise ConflictError(
                f"Function '{function.name}' still has {n} form(s). Delete or migrate them first."
            )
        entries = self._list_volume(function.name)
        if entries is None:
            raise BackendError(
                f"Cannot verify that the file volume of '{function.name}' is empty (no READ VOLUME "
                "privilege, or the Files API is unavailable); the function is not deleted."
            )
        if entries:
            # Anything in the volume counts, not only CSV/Parquet: DROP VOLUME would delete it.
            raise ConflictError(
                f"Function '{function.name}' still has {len(entries)} file(s) or folder(s) in its "
                "volume. Delete or migrate them first."
            )
        self._run(f"DROP VOLUME IF EXISTS {self._s(function.name)}.{_q(FILES_VOLUME)}")
        self._volumes_ready.discard(function.name)
        self._run(f"DROP SCHEMA {self._s(function.name)} RESTRICT")
        try:
            self._run(f"DELETE FROM {self._reg('functions')} WHERE `name` = :name", {"name": function.name})
        except BackendError as exc:
            log.warning("Registry delete for %s failed: %s", function.name, exc)

    # -- registry tables ---------------------------------------------------------------------

    REGISTRY_DDL = {
        "domains": (
            "CREATE TABLE IF NOT EXISTS {t} (name STRING NOT NULL, display_name STRING, description STRING, owner STRING, "
            "created_at TIMESTAMP_NTZ, created_by STRING, updated_at TIMESTAMP_NTZ, updated_by STRING) "
            "USING DELTA COMMENT 'Domains (business classifier above functions) maintained by the Reference Data Manager'"
        ),
        "functions": (
            "CREATE TABLE IF NOT EXISTS {t} (name STRING NOT NULL, domain_name STRING, display_name STRING, "
            "description STRING, owner STRING, owner_email STRING, doc_link STRING, created_at TIMESTAMP_NTZ, "
            "created_by STRING, updated_at TIMESTAMP_NTZ, updated_by STRING) "
            "USING DELTA COMMENT 'Registry of functions (schemas) maintained by the Reference Data Manager'"
        ),
        "forms": (
            "CREATE TABLE IF NOT EXISTS {t} (function_name STRING NOT NULL, name STRING NOT NULL, display_name STRING, "
            "description STRING, owner STRING, owner_email STRING, created_at TIMESTAMP_NTZ, created_by STRING, "
            "updated_at TIMESTAMP_NTZ, updated_by STRING) USING DELTA COMMENT 'Registry of forms maintained by the Reference Data Manager'"
        ),
        "files": (
            "CREATE TABLE IF NOT EXISTS {t} (function_name STRING NOT NULL, name STRING NOT NULL, display_name STRING, "
            "description STRING, owner STRING, owner_email STRING, size_bytes BIGINT, row_count BIGINT, "
            "created_at TIMESTAMP_NTZ, created_by STRING, updated_at TIMESTAMP_NTZ, updated_by STRING) "
            "USING DELTA COMMENT 'Registry of files (CSV/Parquet in the function volumes) maintained by the Reference Data Manager'"
        ),
    }

    def _registry_upsert(
        self, table: str, key: dict[str, Any], values: dict[str, Any], actor: User, strict: bool = False
    ) -> None:
        """Idempotent MERGE into a registry table; creates the table on first use.

        Best effort for the function/form registries (they mirror catalog objects); ``strict``
        for the domain list, which lives only in the registry.
        """
        now = utcnow()
        params = {**key, **values, "now": now, "actor": actor.username}
        on = " AND ".join(f"t.{_q(k)} = :{k}" for k in key)
        set_clause = (
            ", ".join(f"t.{_q(k)} = :{k}" for k in values)
            + ", t.`updated_at` = :now, t.`updated_by` = :actor"
        )
        cols = [*key, *values, "created_at", "created_by", "updated_at", "updated_by"]
        vals = [f":{k}" for k in [*key, *values]] + [":now", ":actor", ":now", ":actor"]
        statement = (
            f"MERGE INTO {self._reg(table)} AS t USING (SELECT 1) AS s ON {on} "
            f"WHEN MATCHED THEN UPDATE SET {set_clause} "
            f"WHEN NOT MATCHED THEN INSERT ({', '.join(_q(c) for c in cols)}) VALUES ({', '.join(vals)})"
        )
        try:
            self._run(statement, params)
        except NotFoundError:
            try:
                self._run(self.REGISTRY_DDL[table].format(t=self._reg(table)))
                self._run(statement, params)
            except BackendError as exc:
                if strict:
                    raise
                log.warning("Registry table %s unavailable: %s", table, exc)
        except BackendError as exc:
            # Registries created before owner_email existed migrate on first write.
            if "owner_email" in str(exc).lower():
                try:
                    self._run(f"ALTER TABLE {self._reg(table)} ADD COLUMNS (`owner_email` STRING)")
                    self._run(statement, params)
                    return
                except BackendError as migrate_exc:
                    exc = migrate_exc
            if strict:
                raise exc
            log.warning("Registry update for %s failed: %s", table, exc)

    def _register_domain(self, domain: DomainDef, actor: User, strict: bool = False) -> None:
        self._registry_upsert(
            "domains",
            {"name": domain.name},
            {
                "display_name": domain.display_name,
                "description": domain.description,
                "owner": domain.owner or actor.username,
            },
            actor,
            strict=strict,
        )

    def _register_function(self, function: FunctionDef, actor: User) -> None:
        self._registry_upsert(
            "functions",
            {"name": function.name},
            {
                "domain_name": function.domain,
                "display_name": function.display_name,
                "description": function.description,
                "owner": function.owner,
                "owner_email": function.owner_email,
                "doc_link": function.doc_link,
            },
            actor,
        )

    def _register_form(self, form: FormDef, actor: User) -> None:
        self._registry_upsert(
            "forms",
            {"function_name": form.function, "name": form.name},
            {
                "display_name": form.display_name,
                "description": form.description,
                "owner": form.owner,
                "owner_email": form.owner_email,
            },
            actor,
        )

    def _unregister_form(self, form: FormDef) -> None:
        try:
            self._run(
                f"DELETE FROM {self._reg('forms')} WHERE `function_name` = :function_name AND `name` = :name",
                {"function_name": form.function, "name": form.name},
            )
        except BackendError as exc:
            log.warning("Registry delete for %s failed: %s", form.full_name, exc)

    def _register_file(self, file: FileDef, actor: User) -> None:
        self._registry_upsert(
            "files",
            {"function_name": file.function, "name": file.name},
            {
                "display_name": file.display_name,
                "description": file.description,
                "owner": file.owner,
                "owner_email": file.owner_email,
                "size_bytes": file.size_bytes,
                "row_count": file.row_count,
            },
            actor,
            strict=True,
        )

    def _unregister_file(self, file: FileDef) -> None:
        try:
            self._run(
                f"DELETE FROM {self._reg('files')} WHERE `function_name` = :function_name AND `name` = :name",
                {"function_name": file.function, "name": file.name},
            )
        except BackendError as exc:
            log.warning("Registry delete for %s failed: %s", file.full_name, exc)

    def _file_registry(self, function: str) -> dict[str, dict[str, Any]]:
        """``SELECT *`` keeps the read tolerant of registries created before newer columns."""
        try:
            rows, cols = self._run(
                f"SELECT * FROM {self._reg('files')} WHERE `function_name` = :function_name",
                {"function_name": function},
            )
        except BackendError:
            return {}
        out = {}
        for r in rows:
            d = dict(zip(cols, r, strict=True))
            out[d["name"]] = d
        return out

    def _file_counts(self) -> dict[str, int]:
        try:
            rows, _ = self._run(
                f"SELECT function_name, count(*) FROM {self._reg('files')} GROUP BY function_name"
            )
        except BackendError:
            return {}
        return {str(f): int(n) for f, n in rows}

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

    def _table_tags(self, function: str) -> dict[str, dict[str, str]]:
        rows, _ = self._run(
            f"SELECT table_name, tag_name, tag_value FROM {self._info('table_tags')} "
            "WHERE catalog_name = :catalog AND schema_name = :schema",
            {"catalog": self.catalog, "schema": function},
        )
        out: dict[str, dict[str, str]] = {}
        for table, k, v in rows:
            out.setdefault(table, {})[k] = v
        return out

    def list_forms(self, function: str) -> list[FormDef]:
        validate_identifier(function, "function name")
        rows, _ = self._run(
            f"SELECT table_name, comment, table_owner, created, last_altered, last_altered_by FROM {self._info('tables')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_type IN ('MANAGED', 'EXTERNAL') "
            "AND table_name NOT LIKE '\\_%' "
            "ORDER BY table_name",
            {"catalog": self.catalog, "schema": function},
        )
        tags = self._table_tags(function)
        forms = []
        for name, comment, owner, created, altered, altered_by in rows:
            t = tags.get(name, {})
            forms.append(
                FormDef(
                    function=function,
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

    def _load_columns(self, function: str, name: str) -> list[ColumnDef]:
        rows, _ = self._run(
            f"SELECT column_name, full_data_type, is_nullable, comment, ordinal_position FROM {self._info('columns')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_name = :table ORDER BY ordinal_position",
            {"catalog": self.catalog, "schema": function, "table": name},
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

    def get_form(self, function: str, name: str) -> FormDef:
        validate_identifier(function, "function name")
        validate_identifier(name, "form name")
        rows, _ = self._run(
            f"SELECT comment, table_owner, created, last_altered, last_altered_by FROM {self._info('tables')} "
            "WHERE table_catalog = :catalog AND table_schema = :schema AND table_name = :table",
            {"catalog": self.catalog, "schema": function, "table": name},
        )
        if not rows:
            raise NotFoundError(f"Form '{function}.{name}' does not exist.")
        comment, owner, created, altered, altered_by = rows[0]
        form = FormDef(
            function=function,
            name=name,
            description=comment or "",
            columns=self._load_columns(function, name),
        )
        form.properties = self._properties(form)
        form.tags = self._table_tags(function).get(name, {})
        form.display_name = form.properties.get(PROP_DISPLAY_NAME) or form.tags.get(TAG_DISPLAY_NAME, "")
        form.owner = form.properties.get(PROP_OWNER) or form.tags.get(TAG_OWNER, "") or (owner or "")
        form.owner_email = form.properties.get(PROP_OWNER_EMAIL, "")
        form.scd2_enabled = form.properties.get(PROP_SCD2, "") == "true"
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
            PROP_OWNER_EMAIL: form.owner_email or "",
            PROP_SCD2: "true" if form.scd2_enabled else "",
            PROP_COLUMN_CONFIG: form.column_config_json(),
        }
        return props

    def create_form(self, form: FormDef, actor: User, rows: pd.DataFrame | None = None) -> FormDef:
        user_cols = [c for c in form.columns if not c.is_system]
        form.columns = system_columns() + user_cols
        form.validate()
        props = {**FORM_TABLE_PROPERTIES, **self._rdm_properties(form, actor)}
        col_ddl = ",\n  ".join(self._column_ddl(c) for c in form.columns)
        prop_ddl = ", ".join(f"{lit(k)} = {lit(v)}" for k, v in props.items() if v)
        self._run(
            f"CREATE TABLE {self._t(form)} (\n  {col_ddl},\n"
            f"  CONSTRAINT {_q('pk_' + form.name)} PRIMARY KEY ({_q(ID_COLUMN)})\n)\n"
            f"USING DELTA\nCOMMENT {lit(form.description or '')}\nTBLPROPERTIES ({prop_ddl})"
        )
        self._set_tags(
            f"ALTER TABLE {self._t(form)}",
            {TAG_FORM: "true", TAG_DISPLAY_NAME: form.display_name, TAG_OWNER: form.owner or actor.username},
        )
        self._register_form(form, actor)
        if form.scd2_enabled:
            self._scd2_create(form)
        if rows is not None and len(rows):
            self._append_rows(form, rows, actor)
        return self.get_form(form.function, form.name)

    def update_form_metadata(self, form: FormDef, actor: User) -> FormDef:
        form.validate()
        current = {c.name: c for c in self._load_columns(form.function, form.name)}
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
        self._register_form(form, actor)
        return self.get_form(form.function, form.name)

    def add_column(self, form: FormDef, column: ColumnDef, actor: User) -> FormDef:
        column.validate()
        if column.is_system:
            raise BackendError("System columns cannot be added manually.")
        if column.name in {c.name for c in self._load_columns(form.function, form.name)}:
            raise ConflictError(f"Column '{column.name}' already exists.")
        column.nullable = True
        comment = f" COMMENT {lit(column.description)}" if column.description else ""
        self._run(
            f"ALTER TABLE {self._t(form)} ADD COLUMN {_q(column.name)} {native_type_databricks(column)}{comment}"
        )
        if form.scd2_enabled:
            self._run(
                f"ALTER TABLE {self._h(form)} ADD COLUMN {_q(column.name)} {native_type_databricks(column)}"
            )
        form.columns.append(column)
        self._run(
            f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({lit(PROP_COLUMN_CONFIG)} = {lit(form.column_config_json())})"
        )
        return self.get_form(form.function, form.name)

    def drop_column(self, form: FormDef, column_name: str, actor: User) -> FormDef:
        if column_name in SYSTEM_COLUMNS:
            raise BackendError("System columns cannot be removed.")
        if column_name not in {c.name for c in self._load_columns(form.function, form.name)}:
            raise NotFoundError(f"Column '{column_name}' does not exist.")
        self._run(f"ALTER TABLE {self._t(form)} DROP COLUMN {_q(column_name)}")
        if form.scd2_enabled:
            self._run(f"ALTER TABLE {self._h(form)} DROP COLUMN {_q(column_name)}")
        form.columns = [c for c in form.columns if c.name != column_name]
        self._run(
            f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({lit(PROP_COLUMN_CONFIG)} = {lit(form.column_config_json())})"
        )
        return self.get_form(form.function, form.name)

    def drop_form(self, form: FormDef, actor: User) -> None:
        self._run(f"DROP TABLE {self._t(form)}")
        self._run(f"DROP TABLE IF EXISTS {self._h(form)}")
        self._unregister_form(form)

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
        applied_ids = [str(o[ID_COLUMN]) for o in ops]

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
        if form.scd2_enabled and result.applied:
            self._scd2_sync(form, applied_ids, now)
        self._write_audit(audit)
        return result

    @staticmethod
    def _plain(row: dict[str, Any], columns: list[ColumnDef]) -> dict[str, Any]:
        out = {ID_COLUMN: row.get(ID_COLUMN)}
        for c in columns:
            out[c.name] = _json_value(c, row.get(c.name))
        return out

    # -- SCD Type 2 history table (FR-47) ---------------------------------------------------

    def _h(self, form: FormDef) -> str:
        return f"{_q(self.catalog)}.{_q(form.function)}.{_q(scd2_table_name(form.name))}"

    def _scd2_create(self, form: FormDef) -> None:
        # Same table properties as an Auto CDC SCD type 2 target: CDF lets
        # consumers chain off the history; column mapping lets it follow DROP COLUMN.
        props = ", ".join(
            f"{lit(k)} = {lit(v)}"
            for k, v in {
                "delta.enableChangeDataFeed": "true",
                "delta.columnMapping.mode": "name",
            }.items()
        )
        self._run(
            f"CREATE TABLE IF NOT EXISTS {self._h(form)} USING DELTA "
            f"COMMENT {lit('Type 2 history of ' + form.full_name + ' maintained by the Reference Data Manager')} "
            f"TBLPROPERTIES ({props}) "
            f"AS SELECT *, CAST(NULL AS TIMESTAMP) AS `{SCD2_START_COLUMN}`, "
            f"CAST(NULL AS TIMESTAMP) AS `{SCD2_END_COLUMN}` FROM {self._t(form)} WHERE 1 = 0"
        )

    def _scd2_sync(self, form: FormDef, row_ids: list[str], now: datetime) -> None:
        """Close and open validity windows for the rows a save touched.

        Works from the form's state: a candidate row whose ``_updated_at`` equals the save
        timestamp was applied (insert or new version); one missing from the form was
        deleted. Rows that lost the concurrency check keep their window untouched.
        """
        for i in range(0, len(row_ids), 200):
            chunk = row_ids[i : i + 200]
            params: dict[str, Any] = {f"p{j}": rid for j, rid in enumerate(chunk)}
            markers = ", ".join(f":{k}" for k in params)
            params["now"] = now
            self._run(
                f"UPDATE {self._h(form)} SET `{SCD2_END_COLUMN}` = :now "
                f"WHERE `{SCD2_END_COLUMN}` IS NULL AND {_q(ID_COLUMN)} IN ({markers}) "
                f"AND ({_q(ID_COLUMN)} NOT IN (SELECT {_q(ID_COLUMN)} FROM {self._t(form)}) "
                f"OR EXISTS (SELECT 1 FROM {self._t(form)} f WHERE f.{_q(ID_COLUMN)} = {self._h(form)}.{_q(ID_COLUMN)} "
                f"AND f.{_q(UPDATED_AT_COLUMN)} = :now))",
                params,
            )
            self._run(
                f"INSERT INTO {self._h(form)} SELECT *, :now, NULL FROM {self._t(form)} "
                f"WHERE {_q(ID_COLUMN)} IN ({markers}) AND {_q(UPDATED_AT_COLUMN)} = :now",
                params,
            )

    def set_scd2(self, form: FormDef, enabled: bool, actor: User) -> FormDef:
        scd2_table_name(form.name)  # length check before anything is written
        if enabled:
            self._scd2_create(form)
            self._run(
                f"INSERT INTO {self._h(form)} SELECT *, {_q(UPDATED_AT_COLUMN)}, NULL FROM {self._t(form)} t "
                f"WHERE NOT EXISTS (SELECT 1 FROM {self._h(form)} h "
                f"WHERE h.{_q(ID_COLUMN)} = t.{_q(ID_COLUMN)} AND h.`{SCD2_END_COLUMN}` IS NULL)"
            )
        self._run(
            f"ALTER TABLE {self._t(form)} SET TBLPROPERTIES ({lit(PROP_SCD2)} = {lit('true' if enabled else '')})"
        )
        return self.get_form(form.function, form.name)

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
        if form.scd2_enabled and total:
            self._scd2_sync(form, [str(a["row_id"]) for a in audit], now)
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
            "schema_name": form.function,
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

    def get_history(self, form: FormDef, limit: int = 200, row_id: str | None = None) -> pd.DataFrame:
        user_cols = [c.name for c in form.user_columns]
        columns = [*HISTORY_COLUMNS, "changed_fields", ID_COLUMN, *user_cols]
        if self._audit_ready is not False:
            try:
                params: dict[str, Any] = {"schema": form.function, "table": form.name}
                row_filter = ""
                if row_id:
                    row_filter = " AND row_id = :row_id"
                    params["row_id"] = row_id
                rows, _ = self._run(
                    f"SELECT changed_at, changed_by, change_type, row_id, before_json, after_json FROM {self._audit()} "
                    f"WHERE schema_name = :schema AND table_name = :table{row_filter} "
                    "ORDER BY changed_at DESC, seq DESC LIMIT " + str(int(limit)),
                    params,
                )
                self._audit_ready = True
                records = []
                n = len(rows)
                for i, (changed_at, changed_by, change_type, rid, before_json, after_json) in enumerate(rows):
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
                        ID_COLUMN: rid,
                    }
                    for c in user_cols:
                        rec[c] = snapshot.get(c)
                    records.append(rec)
                return pd.DataFrame.from_records(records, columns=columns)
            except NotFoundError:
                self._audit_ready = False
        return self._history_from_cdf(form, limit, columns, row_id)

    def _history_from_cdf(
        self, form: FormDef, limit: int, columns: list[str], row_id: str | None = None
    ) -> pd.DataFrame:
        """Fallback: Delta Change Data Feed (bounded by the table's retention settings)."""
        user_cols = [c.name for c in form.user_columns]
        params: dict[str, Any] = {}
        row_filter = ""
        if row_id:
            row_filter = f" AND {_q(ID_COLUMN)} = :row_id"
            params["row_id"] = row_id
        try:
            hist, _ = self._run(f"DESCRIBE HISTORY {self._t(form)}")
            versions = [int(r[0]) for r in hist] if hist else [0]
            start = max(0, min(versions))
            df = self._run_df(
                f"SELECT _change_type, _commit_version, _commit_timestamp, {', '.join(_q(c) for c in [ID_COLUMN, UPDATED_BY_COLUMN, *user_cols])} "
                f"FROM table_changes({lit(f'{self.catalog}.{form.function}.{form.name}')}, {start}) "
                f"WHERE _change_type <> 'update_preimage'{row_filter} ORDER BY _commit_version DESC LIMIT "
                + str(int(limit)),
                params or None,
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

    # -- files (managed volume per function) -----------------------------------------------

    def _volume_path(self, function: str) -> str:
        validate_identifier(function, "function name", allow_leading_underscore=False)
        return self._assert_volume_path(f"/Volumes/{self.catalog}/{function}/{FILES_VOLUME}")

    def _file_path(self, function: str, name: str) -> str:
        return self._assert_volume_path(volume_file_path(self.catalog, function, name))

    def _ensure_volume(self, function: str) -> None:
        if function in self._volumes_ready:
            return
        self._run(
            f"CREATE VOLUME IF NOT EXISTS {self._s(function)}.{_q(FILES_VOLUME)} "
            "COMMENT 'Files (CSV/Parquet) of this function, managed by the Reference Data Manager'"
        )
        self._volumes_ready.add(function)

    def _list_volume(self, function: str) -> list[Any] | None:
        """Raw entries of the function's volume: ``[]`` when there is no volume (yet), ``None``
        when it exists but cannot be listed (no READ VOLUME, Files API error)."""
        try:
            return list(self._files_api().list_directory_contents(self._volume_path(function)))
        except Exception as exc:  # noqa: BLE001 - SDK NotFound or a transport/permission error
            text = str(exc).upper().replace("_", " ")
            if type(exc).__name__ in ("NotFound", "ResourceDoesNotExist") or "NOT FOUND" in text:
                log.info("No file volume for %s yet: %s", function, exc)
                return []
            log.warning("Cannot list the file volume of %s: %s", function, exc)
            return None

    def _stored_files(self, function: str) -> dict[str, dict[str, Any]]:
        """``{name: {size, modified}}`` of the CSV/Parquet files in the function's volume
        (empty when the volume is absent or cannot be listed)."""
        out: dict[str, dict[str, Any]] = {}
        for e in self._list_volume(function) or []:
            if getattr(e, "is_directory", False):
                continue
            name = str(getattr(e, "name", "") or "")
            if split_file_name(name)[1] not in FILE_FORMATS:
                continue
            out[name] = {"size": getattr(e, "file_size", None), "modified": getattr(e, "last_modified", None)}
        return out

    def _file_from(
        self, function: str, name: str, stored: dict[str, Any] | None, reg: dict[str, Any] | None
    ) -> FileDef:
        reg = reg or {}
        size = (stored or {}).get("size")
        if size is None:
            size = reg.get("size_bytes")
        return FileDef(
            function=function,
            name=name,
            display_name=reg.get("display_name") or "",
            description=reg.get("description") or "",
            owner=reg.get("owner") or "",
            owner_email=reg.get("owner_email") or "",
            size_bytes=int(size) if size is not None else None,
            row_count=int(reg["row_count"]) if reg.get("row_count") is not None else None,
            path=self._file_path(function, name),
            registered=bool(reg),
            created_at=to_db_scalar(reg.get("created_at")),
            created_by=reg.get("created_by") or "",
            updated_at=to_db_scalar(reg.get("updated_at")),
            updated_by=reg.get("updated_by") or "",
        )

    def _reader(self, file: FileDef) -> str:
        path = lit(self._file_path(file.function, file.name))
        if file.format == "parquet":
            return f"read_files({path}, format => 'parquet')"
        return f"read_files({path}, format => 'csv', header => true)"

    def list_files(self, function: str) -> list[FileDef]:
        stored = self._stored_files(function)
        registry = self._file_registry(function)
        return [self._file_from(function, n, stored[n], registry.get(n)) for n in sorted(stored)]

    def _metadata(self, function: str, name: str) -> dict[str, Any] | None:
        try:
            meta = self._files_api().get_metadata(self._file_path(function, name))
        except Exception:  # noqa: BLE001 - not found (or not readable)
            return None
        return {
            "size": getattr(meta, "content_length", None),
            "modified": getattr(meta, "last_modified", None),
        }

    def get_file(self, function: str, name: str) -> FileDef:
        stored = self._metadata(function, name)
        if stored is None:
            raise NotFoundError(f"File '{function}/{name}' does not exist.")
        return self._file_from(function, name, stored, self._file_registry(function).get(name))

    def put_file(self, file: FileDef, data: bytes, actor: User, replace: bool = False) -> FileDef:
        file.validate()
        existing = self._metadata(file.function, file.name)
        if existing is not None and not replace:
            raise ConflictError(f"File '{file.full_name}' already exists. Replace it from its page instead.")
        if replace and existing is None:
            raise NotFoundError(f"File '{file.full_name}' does not exist.")
        previous = (
            self._file_from(
                file.function, file.name, existing, self._file_registry(file.function).get(file.name)
            )
            if existing is not None
            else None
        )
        self._ensure_volume(file.function)
        path = self._file_path(file.function, file.name)
        try:
            # overwrite only on an explicit replace: a new file can never clobber one that the
            # existence check above missed (for example because of a transient metadata error).
            self._files_api().upload(path, io.BytesIO(data), overwrite=replace)
        except Exception as exc:  # noqa: BLE001
            if "EXISTS" in str(exc).upper() or type(exc).__name__ == "AlreadyExists":
                raise ConflictError(
                    f"File '{file.full_name}' already exists. Replace it from its page instead."
                ) from exc
            raise BackendError(f"Upload to {path} failed: {exc}") from exc
        try:
            rows, _ = self._run(f"SELECT count(*) FROM {self._reader(file)}")
        except BackendError as exc:
            if previous is None:
                try:
                    self._files_api().delete(path)
                except Exception:  # noqa: BLE001
                    log.warning("Could not remove unreadable upload %s", path)
            raise BackendError(f"The file cannot be read as {file.format.upper()}: {exc}") from exc
        file.size_bytes = len(data)
        file.row_count = int(rows[0][0]) if rows else None
        if previous is not None:
            file.display_name = file.display_name or previous.display_name
            file.description = file.description or previous.description
            file.owner = file.owner or previous.owner
            file.owner_email = file.owner_email or previous.owner_email
        file.owner = file.owner or actor.username
        self._register_file(file, actor)
        now = utcnow()
        self._write_audit(
            [
                self._audit_row(
                    file,
                    None,
                    "replace" if previous is not None else "upload",
                    actor,
                    uuid.uuid4().hex,
                    {"size_bytes": previous.size_bytes, "row_count": previous.row_count}
                    if previous
                    else None,
                    {"size_bytes": file.size_bytes, "row_count": file.row_count, "format": file.format},
                    now,
                    0,
                )
            ]
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
        self._register_file(current, actor)
        return self.get_file(file.function, file.name)

    def read_file(self, file: FileDef) -> bytes:
        try:
            response = self._files_api().download(self._file_path(file.function, file.name))
            return response.contents.read()
        except Exception as exc:  # noqa: BLE001
            raise NotFoundError(f"File '{file.full_name}' could not be downloaded: {exc}") from exc

    def preview_file(self, file: FileDef, limit: int = 100) -> pd.DataFrame:
        return self._run_df(f"SELECT * FROM {self._reader(file)} LIMIT {int(limit)}")

    def file_columns(self, file: FileDef) -> list[ColumnDef]:
        rows, _ = self._run(f"DESCRIBE QUERY SELECT * FROM {self._reader(file)}")
        cols = []
        for i, row in enumerate(rows):
            cname, dtype = str(row[0]), str(row[1])
            t, p, s = parse_native_type(dtype)
            cols.append(
                ColumnDef(
                    name=cname, data_type=t, precision=p, scale=s, native_type=dtype.upper(), position=i
                )
            )
        return cols

    def file_history(self, file: FileDef, limit: int = 200) -> pd.DataFrame:
        columns = ["version", "changed_at", "changed_by", "change_type", "size_bytes", "row_count"]
        kinds = ", ".join(lit(k) for k in FILE_CHANGE_TYPES)
        try:
            rows, _ = self._run(
                f"SELECT changed_at, changed_by, change_type, before_json, after_json FROM {self._audit()} "
                f"WHERE schema_name = :schema AND table_name = :table AND change_type IN ({kinds}) "
                "ORDER BY changed_at DESC, seq DESC LIMIT " + str(int(limit)),
                {"schema": file.function, "table": file.name},
            )
        except NotFoundError:
            return pd.DataFrame(columns=columns)
        records = []
        n = len(rows)
        for i, (changed_at, changed_by, change_type, before_json, after_json) in enumerate(rows):
            snapshot = (
                json.loads(after_json) if after_json else (json.loads(before_json) if before_json else {})
            )
            records.append(
                {
                    "version": n - i,
                    "changed_at": to_db_scalar(changed_at),
                    "changed_by": changed_by,
                    "change_type": change_type,
                    "size_bytes": snapshot.get("size_bytes"),
                    "row_count": snapshot.get("row_count"),
                }
            )
        return pd.DataFrame.from_records(records, columns=columns)

    def drop_file(self, file: FileDef, actor: User) -> None:
        current = self.get_file(file.function, file.name)
        try:
            self._files_api().delete(self._file_path(file.function, file.name))
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"Could not delete {file.full_name}: {exc}") from exc
        self._unregister_file(file)
        self._write_audit(
            [
                self._audit_row(
                    file,
                    None,
                    "delete",
                    actor,
                    uuid.uuid4().hex,
                    {"size_bytes": current.size_bytes, "row_count": current.row_count, "format": file.format},
                    None,
                    utcnow(),
                    0,
                )
            ]
        )

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
        owner_rows, _ = self._run(
            f"SELECT catalog_owner FROM {self._info('catalogs')} WHERE catalog_name = :catalog",
            {"catalog": self.catalog},
        )
        catalog_owners = {str(r[0]) for r in owner_rows if r and r[0]}
        is_global_admin = bool(
            {p.replace("_", " ") for p in catalog_privs}
            & {p.replace("_", " ") for p in GLOBAL_ADMIN_PRIVILEGES}
        )
        if not is_global_admin and catalog_owners:
            is_global_admin = self._is_principal(user, catalog_owners)
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
        return Permissions(function_roles=roles, is_global_admin=is_global_admin)

    def _is_principal(self, user: User, names: set[str]) -> bool:
        """Whether the user is (a member of) one of ``names``; uses the SQL session under OBO."""
        if names & set(user.principals):
            return True
        if not self.access_token:
            return False
        checks = " OR ".join(f"is_account_group_member({lit(n)})" for n in sorted(names)) or "false"
        try:
            rows, _ = self._run(f"SELECT {checks}")
            return bool(rows and rows[0][0])
        except BackendError:
            return False

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

    def list_function_grants(self, function: str) -> list[tuple[str, Role]]:
        validate_identifier(function, "function name")
        rows, _ = self._run(
            f"SELECT grantee, privilege_type FROM {self._info('schema_privileges')} "
            "WHERE catalog_name = :catalog AND schema_name = :schema",
            {"catalog": self.catalog, "schema": function},
        )
        by_grantee: dict[str, set[str]] = {}
        for grantee, priv in rows:
            by_grantee.setdefault(str(grantee), set()).add(str(priv).upper())
        return sorted((g, self._role_from_privileges(p)) for g, p in by_grantee.items())

    def list_groups(self, query: str | None = None) -> list[str]:
        """Account/workspace groups via the SDK (app identity). Empty when the lookup is not possible."""
        try:
            from databricks.sdk import WorkspaceClient

            w = WorkspaceClient()
            needle = (query or "").strip().replace('"', "")
            flt = f'displayName co "{needle}"' if needle else None
            groups = [
                g.display_name
                for g in w.groups.list(filter=flt, attributes="displayName", count=200)
                if g.display_name
            ]
            return sorted(set(groups))
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("Group lookup failed: %s", exc)
            return []

    def grant_function_role(self, function: str, principal: str, role: Role, actor: User) -> None:
        """Replace the app-managed schema privileges of a *group* with the set for ``role``.

        Requires MANAGE on the schema (function admin). ``USE CATALOG`` is also granted so the
        group can reach the schema; that part needs catalog rights and is best effort.
        """
        validate_identifier(function, "function name")
        principal = (principal or "").strip()
        if not principal:
            raise BackendError("A group is required.")
        if "@" in principal:
            raise BackendError("Access is granted to Databricks groups only, not to individual users.")
        known = self.list_groups(principal)
        if known and principal not in known:
            raise BackendError(f"'{principal}' is not a Databricks group.")
        grantee = "`" + principal.replace("`", "``") + "`"
        schema = self._s(function)
        self._run(f"REVOKE {', '.join(MANAGED_PRIVILEGES)} ON SCHEMA {schema} FROM {grantee}")
        if role is Role.NONE:
            return
        self._run(f"GRANT {', '.join(ROLE_PRIVILEGES[role])} ON SCHEMA {schema} TO {grantee}")
        try:
            self._run(f"GRANT USE CATALOG ON CATALOG {_q(self.catalog)} TO {grantee}")
        except BackendError as exc:
            log.warning(
                "Could not grant USE CATALOG to %s (%s); a global admin must grant it", principal, exc
            )
