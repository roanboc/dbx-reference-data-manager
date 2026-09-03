"""SQL generation of the Databricks backend against a fake connection (no warehouse needed)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pandas as pd
import pytest

from rdm.backend.base import BackendError, ConflictError, NotFoundError
from rdm.backend.databricks_backend import DatabricksBackend
from rdm.models import (
    ID_COLUMN,
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
    system_columns,
)


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.calls.append((sql, params))
        for pattern, cols, rows in self.conn.responses:
            if re.search(pattern, sql, re.S | re.I):
                rows = rows(sql, params) if callable(rows) else rows
                self.description = [(c,) for c in cols] if cols else None
                self._rows = list(rows)
                return self
        self.description = None
        self._rows = []
        return self

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConnection:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.calls = []
        self.closed = False

    def cursor(self):
        return FakeCursor(self)

    def close(self):
        self.closed = True


class FakeEntry:
    def __init__(self, path, size=None, is_directory=False):
        self.path = path
        self.name = path.rsplit("/", 1)[-1]
        self.file_size = size
        self.last_modified = 1
        self.is_directory = is_directory


class FakeMeta:
    def __init__(self, size):
        self.content_length = size
        self.last_modified = 1


class FakeDownload:
    def __init__(self, data):
        self.contents = __import__("io").BytesIO(data)


class FakeFiles:
    """Stand-in for the SDK Files API: an in-memory volume."""

    def __init__(self):
        self.store: dict[str, bytes] = {}
        self.calls: list[tuple] = []

    def list_directory_contents(self, path):
        self.calls.append(("list", path))
        prefix = path.rstrip("/") + "/"
        if not any(p.startswith(prefix) for p in self.store):
            raise RuntimeError("NOT_FOUND")
        return [FakeEntry(p, len(d)) for p, d in self.store.items() if p.startswith(prefix)] + [
            FakeEntry(prefix + "sub", is_directory=True)
        ]

    def get_metadata(self, path):
        self.calls.append(("meta", path))
        if path not in self.store:
            raise RuntimeError("NOT_FOUND")
        return FakeMeta(len(self.store[path]))

    def upload(self, path, contents, overwrite=False):
        self.calls.append(("upload", path, overwrite))
        if path in self.store and not overwrite:
            raise RuntimeError("ALREADY_EXISTS: the file exists and overwrite is false")
        self.store[path] = contents.read()

    def download(self, path):
        self.calls.append(("download", path))
        if path not in self.store:
            raise RuntimeError("NOT_FOUND")
        return FakeDownload(self.store[path])

    def delete(self, path):
        self.calls.append(("delete", path))
        self.store.pop(path)


def make_backend(responses=None, token="tok", files=None) -> tuple[DatabricksBackend, FakeConnection]:
    conn = FakeConnection(responses)
    files = files if files is not None else FakeFiles()
    b = DatabricksBackend(
        catalog="_reference_data",
        http_path="/sql/1.0/warehouses/abc",
        access_token=token,
        connection_factory=lambda: conn,
        files_client_factory=lambda: files,
    )
    b._fake_files = files  # type: ignore[attr-defined]
    return b, conn


def sample_form() -> FormDef:
    return FormDef(
        "finance__cost",
        "cost_centres",
        display_name="Cost Centres",
        description="It's the 'main' list",
        owner="fin@example.org",
        columns=system_columns()
        + [
            ColumnDef("code", DataType.STRING, "Finance code", nullable=False, is_key=True),
            ColumnDef("name", DataType.STRING, "Name"),
            ColumnDef("budget", DataType.DECIMAL, precision=18, scale=2),
            ColumnDef("active", DataType.BOOLEAN),
            ColumnDef("valid_from", DataType.DATE),
            ColumnDef("seen", DataType.TIMESTAMP),
            ColumnDef("category", DataType.STRING, options=["A", "B"]),
        ],
    )


ADMIN = User("alice@example.org", groups=("rdm_admins",))


def statements(conn, pattern):
    return [(s, p) for s, p in conn.calls if re.search(pattern, s, re.S | re.I)]


# -- naming and safety ------------------------------------------------------------------------


def test_identifiers_are_validated_and_backquoted():
    b, _ = make_backend()
    assert b._t(sample_form()) == "`_reference_data`.`finance__cost`.`cost_centres`"
    with pytest.raises(ValueError):
        DatabricksBackend(catalog="bad name", http_path="/x", connection_factory=lambda: None)
    with pytest.raises(ValueError):
        b.list_forms("Robert'); DROP TABLE x;--")


def test_describe_mentions_mode():
    b, _ = make_backend(token=None)
    assert "service principal" in b.describe()
    b2, _ = make_backend()
    assert "as you" in b2.describe()


# -- DDL -----------------------------------------------------------------------------------


def test_create_form_emits_single_create_table_with_features_and_escaped_comments():
    form = sample_form()
    responses = [
        (
            r"information_schema`\.`tables`.*table_name = :table",
            ["comment", "table_owner", "created", "last_altered", "last_altered_by"],
            [("It's the 'main' list", "alice", None, None, None)],
        ),
        (
            r"information_schema`\.`columns`",
            ["column_name", "full_data_type", "is_nullable", "comment", "ordinal_position"],
            [(c.name, "string", "YES", "", i) for i, c in enumerate(form.columns)],
        ),
        (r"SHOW TBLPROPERTIES", ["key", "value"], [("rdm.display_name", "Cost Centres")]),
        (r"SELECT count\(\*\)", ["c"], [(0,)]),
    ]
    b, conn = make_backend(responses)
    created = b.create_form(form, ADMIN)
    assert created.display_name == "Cost Centres"
    [(ddl, params)] = statements(conn, r"^CREATE TABLE")
    assert params is None
    assert ddl.startswith("CREATE TABLE `_reference_data`.`finance__cost`.`cost_centres` (")
    assert "`_id` STRING NOT NULL" in ddl and "`_version` BIGINT NOT NULL" in ddl
    assert "`code` STRING NOT NULL COMMENT 'Finance code'" in ddl
    assert "`budget` DECIMAL(18,2)" in ddl and "`seen` TIMESTAMP" in ddl
    assert "PRIMARY KEY (`_id`)" in ddl and "USING DELTA" in ddl
    assert "COMMENT 'It\\'s the \\'main\\' list'" in ddl  # Spark escaping, never '' doubling
    assert "''" not in ddl
    assert "'delta.enableChangeDataFeed' = 'true'" in ddl and "'delta.columnMapping.mode' = 'name'" in ddl
    config = '{"columns":{"category":{"options":["A","B"]},"code":{"key":true}},"version":1}'
    assert f"'rdm.column_config' = '{config}'" in ddl
    assert statements(conn, r"SET TAGS")  # tags are set (best effort)


FUNCTION_REGISTRY_COLS = ["name", "domain_name", "display_name", "description", "owner", "doc_link"]
DOMAIN_REGISTRY = (
    r"SELECT name, display_name, description, owner FROM `_reference_data`.`_catalog`.`domains`",
    ["name", "display_name", "description", "owner"],
    [("research", "Research", "Research data", "r@example.org")],
)


def test_create_function_issues_create_schema_with_domain_and_registers_it():
    responses = [
        (
            r"schemata.*WHERE catalog_name = :catalog ORDER BY",
            ["schema_name", "comment", "schema_owner"],
            [("research__rimu", "RIMU lists", "alice")],
        ),
        (
            r"SELECT name, domain_name, display_name, description, owner, doc_link FROM",
            FUNCTION_REGISTRY_COLS,
            [
                (
                    "research__rimu",
                    "research",
                    "Research - RIMU",
                    "RIMU lists",
                    "r@example.org",
                    "https://wiki/rimu",
                )
            ],
        ),
        DOMAIN_REGISTRY,
    ]
    b, conn = make_backend(responses)
    function = b.create_function(
        FunctionDef(
            "research__rimu",
            display_name="Research - RIMU",
            description="RIMU lists",
            owner="r@example.org",
            doc_link="https://wiki/rimu",
            domain="research",
        ),
        ADMIN,
    )
    [(ddl, _)] = statements(conn, r"^CREATE SCHEMA")
    assert ddl.startswith(
        "CREATE SCHEMA `_reference_data`.`research__rimu` COMMENT 'RIMU lists' WITH DBPROPERTIES ("
    )
    assert "'rdm.doc_link' = 'https://wiki/rimu'" in ddl and "'rdm.display_name' = 'Research - RIMU'" in ddl
    assert "'rdm.domain' = 'research'" in ddl
    [(tags, _)] = statements(conn, r"ALTER SCHEMA .* SET TAGS")
    assert "'rdm_domain' = 'research'" in tags
    [(merge, params)] = statements(conn, r"^MERGE INTO `_reference_data`.`_catalog`.`functions`")
    assert params["doc_link"] == "https://wiki/rimu" and params["name"] == "research__rimu"
    assert params["domain_name"] == "research"
    assert function.doc_link == "https://wiki/rimu" and function.display_name == "Research - RIMU"
    assert function.domain == "research"
    # an unknown domain is refused before any DDL runs
    conn.calls.clear()
    with pytest.raises(NotFoundError, match="Domain 'ghost' does not exist"):
        b.create_function(FunctionDef("x", domain="ghost"), ADMIN)
    assert not statements(conn, r"^CREATE SCHEMA")


def test_function_domain_falls_back_to_the_schema_tag():
    responses = [
        (
            r"schemata.*WHERE catalog_name = :catalog ORDER BY",
            ["schema_name", "comment", "schema_owner"],
            [("hr__reference", "", "alice"), ("information_schema", "", "sys")],
        ),
        (
            r"schema_tags",
            ["schema_name", "tag_name", "tag_value"],
            [("hr__reference", "rdm_domain", "people"), ("hr__reference", "rdm_display_name", "HR")],
        ),
    ]
    b, _ = make_backend(responses)
    [f] = b.list_functions()
    assert f.name == "hr__reference" and f.domain == "people" and f.display_name == "HR"


def test_domain_registry_crud_and_delete_guard():
    responses = [
        DOMAIN_REGISTRY,
        (
            r"schemata.*WHERE catalog_name = :catalog ORDER BY",
            ["schema_name", "comment", "schema_owner"],
            [("research__rimu", "", "alice")],
        ),
        (
            r"SELECT name, domain_name, display_name, description, owner, doc_link FROM",
            FUNCTION_REGISTRY_COLS,
            [("research__rimu", "research", "", "", "", "")],
        ),
    ]
    b, conn = make_backend(responses)
    [d] = b.list_domains()
    assert d.name == "research" and d.title == "Research" and d.function_count == 1
    assert b.get_domain("research").owner == "r@example.org"
    with pytest.raises(NotFoundError):
        b.get_domain("ghost")
    with pytest.raises(BackendError, match="already exists"):
        b.create_domain(DomainDef("research"), ADMIN)
    with pytest.raises(BackendError, match="still has 1 function"):
        b.delete_domain("research", ADMIN)
    with pytest.raises(NotFoundError):
        b.delete_domain("ghost", ADMIN)
    with pytest.raises(NotFoundError):
        b.update_domain(DomainDef("ghost"), ADMIN)
    b.update_domain(DomainDef("research", "Research & Innovation", "d", "o@example.org"), ADMIN)
    [(merge, params)] = statements(conn, r"^MERGE INTO `_reference_data`.`_catalog`.`domains`")
    assert params["display_name"] == "Research & Innovation" and params["owner"] == "o@example.org"
    assert "doc_link" not in params
    conn.calls.clear()
    # creating a new domain writes the registry, strictly (a failure surfaces)
    b, conn = make_backend(
        [DOMAIN_REGISTRY, (r"^MERGE INTO", None, lambda s, p: (_ for _ in ()).throw(RuntimeError("boom")))]
    )
    with pytest.raises(BackendError, match="boom"):
        b.create_domain(DomainDef("student", "Student"), ADMIN)


def test_delete_domain_deletes_from_the_registry():
    responses = [DOMAIN_REGISTRY, (r"schemata.*ORDER BY", ["schema_name", "comment", "schema_owner"], [])]
    b, conn = make_backend(responses)
    b.delete_domain("research", ADMIN)
    [(sql, params)] = statements(conn, r"^DELETE FROM `_reference_data`.`_catalog`.`domains`")
    assert params == {"name": "research"}


def test_drop_function_refuses_non_empty_schema_then_drops_and_unregisters():
    b, conn = make_backend([(r"SELECT count\(\*\) FROM .*information_schema.*tables", ["c"], [(2,)])])
    with pytest.raises(BackendError, match="still has 2 form"):
        b.drop_function(FunctionDef("finance__cost"), ADMIN)
    assert not statements(conn, r"^DROP SCHEMA")
    b, conn = make_backend([(r"SELECT count\(\*\) FROM .*information_schema.*tables", ["c"], [(0,)])])
    b.drop_function(FunctionDef("finance__cost"), ADMIN)
    assert statements(conn, r"^DROP SCHEMA `_reference_data`.`finance__cost` RESTRICT$")
    assert not statements(conn, r"CASCADE")
    [(sql, params)] = statements(conn, r"^DELETE FROM `_reference_data`.`_catalog`.`functions`")
    assert params == {"name": "finance__cost"}


def test_grant_domain_role_revokes_then_grants_group_privileges():
    b, conn = make_backend()
    b.grant_function_role("finance__cost", "finance stewards", Role.EDITOR, ADMIN)
    [(revoke, _)] = statements(conn, r"^REVOKE")
    assert (
        revoke
        == "REVOKE USE SCHEMA, SELECT, READ VOLUME, MODIFY, WRITE VOLUME, CREATE TABLE, CREATE VOLUME, MANAGE, APPLY TAG "
        "ON SCHEMA `_reference_data`.`finance__cost` FROM `finance stewards`"
    )
    grants = [s for s, _ in statements(conn, r"^GRANT")]
    assert (
        grants[0]
        == "GRANT USE SCHEMA, SELECT, READ VOLUME, MODIFY, WRITE VOLUME ON SCHEMA `_reference_data`.`finance__cost` TO `finance stewards`"
    )
    assert grants[1] == "GRANT USE CATALOG ON CATALOG `_reference_data` TO `finance stewards`"
    conn.calls.clear()
    b.grant_function_role("finance__cost", "finance_readers", Role.NONE, ADMIN)
    assert len(statements(conn, r"^REVOKE")) == 1 and not statements(conn, r"^GRANT")
    with pytest.raises(BackendError, match="groups only"):
        b.grant_function_role("finance__cost", "someone@example.org", Role.VIEWER, ADMIN)
    with pytest.raises(BackendError, match="group is required"):
        b.grant_function_role("finance__cost", " ", Role.VIEWER, ADMIN)


def test_drop_column_and_add_column_statements():
    form = sample_form()
    cols = [(c.name, "string", "YES", "", i) for i, c in enumerate(form.columns)]
    responses = [
        (
            r"information_schema`\.`columns`",
            ["column_name", "full_data_type", "is_nullable", "comment", "ordinal_position"],
            cols,
        ),
        (
            r"information_schema`\.`tables`",
            ["comment", "table_owner", "created", "last_altered", "last_altered_by"],
            [("", "a", None, None, None)],
        ),
        (r"SELECT count", ["c"], [(1,)]),
    ]
    b, conn = make_backend(responses)
    b.add_column(form, ColumnDef("notes", DataType.STRING, "Free text"), ADMIN)
    assert statements(conn, r"ALTER TABLE .* ADD COLUMN `notes` STRING COMMENT 'Free text'")
    with pytest.raises(BackendError):
        b.drop_column(form, ID_COLUMN, ADMIN)
    with pytest.raises(NotFoundError):
        b.drop_column(form, "missing", ADMIN)
    b.drop_column(form, "name", ADMIN)
    assert statements(conn, r"ALTER TABLE .* DROP COLUMN `name`")


# -- reads -----------------------------------------------------------------------------------


def test_read_rows_search_order_and_normalisation():
    form = sample_form()
    now = datetime(2024, 1, 1, 12, 0)
    row = ("r1", 2, now, "a", now, "a", "C1", "Name", "12.5", True, "2024-02-03", now, "A")
    responses = [(r"^SELECT .* FROM `_reference_data`", [c.name for c in form.columns], [row])]
    b, conn = make_backend(responses)
    df = b.read_rows(form, search="50% of_x", limit=10, order_by="name", descending=True)
    [(sql, params)] = statements(conn, r"^SELECT .* FROM `_reference_data`")
    assert "CAST(`code` AS STRING) ILIKE :pattern ESCAPE '\\\\'" in sql
    assert params == {"pattern": "%50\\% of\\_x%"}
    assert "ORDER BY `name` DESC NULLS LAST, `_id`" in sql and sql.endswith("LIMIT 10")
    assert "`_created_at`" not in sql.split("WHERE")[1]  # system columns are not searched
    assert str(df["budget"].iloc[0]) == "12.50" and df["valid_from"].iloc[0].isoformat() == "2024-02-03"
    assert str(df[VERSION_COLUMN].dtype) == "Int64"


def test_read_rows_default_order_uses_business_key():
    form = sample_form()
    b, conn = make_backend([(r"^SELECT", [c.name for c in form.columns], [])])
    b.read_rows(form)
    [(sql, _)] = statements(conn, r"^SELECT")
    assert "ORDER BY `code` ASC NULLS LAST, `_id`" in sql and "WHERE" not in sql


# -- writes ----------------------------------------------------------------------------------


def current_rows_response(rows):
    cols = [c.name for c in sample_form().columns]
    return (r"WHERE `_id` IN", cols, rows)


def test_apply_changes_is_one_merge_with_json_payload_and_audit_insert():
    form = sample_form()
    now = datetime(2024, 1, 1)
    existing = ("r1", 3, now, "seed", now, "seed", "C1", "Old", "1.00", False, "2024-01-01", now, "A")
    responses = [
        current_rows_response([existing]),
        (
            r"^MERGE INTO",
            ["num_affected_rows", "num_updated_rows", "num_deleted_rows", "num_inserted_rows"],
            [(2, 1, 0, 1)],
        ),
    ]
    b, conn = make_backend(responses)
    changes = ChangeSet(
        inserts=[
            RowInsert(
                {
                    "code": "C9",
                    "name": "New",
                    "budget": "5.5",
                    "active": True,
                    "valid_from": "2024-05-01",
                    "category": "B",
                },
                "New row 1",
            )
        ],
        updates=[RowUpdate("r1", {"name": "Renamed", "budget": "2.25"}, 3, "Row code=C1")],
    )
    result = b.apply_changes(form, changes, ADMIN)
    assert (result.inserted, result.updated, result.deleted, result.conflicts) == (1, 1, 0, [])
    [(merge, params)] = statements(conn, r"^MERGE INTO")
    assert (
        "USING (SELECT inline(from_json(:payload, 'ARRAY<STRUCT<_op:STRING,_id:STRING,_version:BIGINT,code:STRING,name:STRING,budget:DECIMAL(18,2),active:BOOLEAN,valid_from:DATE,seen:TIMESTAMP,category:STRING>>'"
        in merge
    )
    assert "WHEN MATCHED AND s.`_op` = 'D' AND t.`_version` = s.`_version` THEN DELETE" in merge
    assert (
        "WHEN MATCHED AND s.`_op` = 'U' AND t.`_version` = s.`_version` THEN UPDATE SET t.`code` = s.`code`"
        in merge
    )
    assert "t.`_version` = t.`_version` + 1, t.`_updated_at` = :now, t.`_updated_by` = :actor" in merge
    assert (
        "WHEN NOT MATCHED AND s.`_op` = 'I' THEN INSERT (`_id`, `_version`, `_created_at`, `_created_by`, `_updated_at`, `_updated_by`, `code`"
        in merge
    )
    payload = json.loads(params["payload"])
    assert params["actor"] == ADMIN.username and params["now"].tzinfo is not None
    ins = next(o for o in payload if o["_op"] == "I")
    assert (
        ins["code"] == "C9"
        and ins["budget"] == "5.5"
        and ins["valid_from"] == "2024-05-01"
        and ins[VERSION_COLUMN] == 1
    )
    upd = next(o for o in payload if o["_op"] == "U")
    # the update carries the full row (unchanged columns merged from the current values)
    assert (
        upd[ID_COLUMN] == "r1"
        and upd[VERSION_COLUMN] == 3
        and upd["name"] == "Renamed"
        and upd["code"] == "C1"
        and upd["budget"] == "2.25"
    )
    assert upd["seen"].startswith("2024-01-01T00:00:00")
    [(audit, aparams)] = statements(conn, r"^INSERT INTO `_reference_data`.`_catalog`.`change_log`")
    entries = json.loads(aparams["payload"])
    assert [e["change_type"] for e in entries] == ["insert", "update"]
    assert (
        json.loads(entries[1]["before_json"])["name"] == "Old"
        and json.loads(entries[1]["after_json"])["name"] == "Renamed"
    )


def test_apply_changes_reports_stale_and_missing_rows_as_conflicts_without_merging_them():
    form = sample_form()
    now = datetime(2024, 1, 1)
    existing = ("r1", 5, now, "bob", now, "bob", "C1", "Old", None, None, None, None, None)
    b, conn = make_backend(
        [
            current_rows_response([existing]),
            (
                r"^MERGE INTO",
                ["num_affected_rows", "num_updated_rows", "num_deleted_rows", "num_inserted_rows"],
                [(0, 0, 0, 0)],
            ),
        ]
    )
    changes = ChangeSet(
        updates=[RowUpdate("r1", {"name": "x"}, 3, "Row code=C1")], deletes=[RowDelete("gone", 1, "ghost")]
    )
    result = b.apply_changes(form, changes, ADMIN)
    assert result.applied == 0
    assert result.conflicts == [
        "Row code=C1: modified by bob at 2024-01-01 00:00:00 UTC after you loaded it.",
        "ghost: the row was deleted by someone else.",
    ]
    assert not statements(conn, r"^MERGE INTO")  # nothing left to apply


def test_append_rows_uses_from_json_insert_in_chunks():
    form = sample_form()
    b, conn = make_backend()
    rows = pd.DataFrame(
        {"code": [f"C{i}" for i in range(1200)], "name": ["n"] * 1200, "budget": [1.5] * 1200}
    )
    assert b.append_rows(form, rows, ADMIN) == 1200
    inserts = statements(conn, r"^INSERT INTO `_reference_data`.`finance__cost`.`cost_centres`")
    assert len(inserts) == 3  # 500-row chunks
    sql, params = inserts[0]
    assert "SELECT s.`_id`, 1, :now, :actor, :now, :actor, s.`code`" in sql and "from_json(:payload" in sql
    payload = json.loads(params["payload"])
    assert len(payload) == 500 and payload[0]["budget"] == "1.5" and payload[0][ID_COLUMN]


def test_audit_table_is_created_on_demand_and_history_reads_it():
    form = sample_form()
    state = {"created": False}

    def audit_rows(sql, params):
        return []

    responses = [
        (
            r"^INSERT INTO `_reference_data`.`_catalog`",
            None,
            lambda sql, params: (
                (_ for _ in ()).throw(RuntimeError("TABLE_OR_VIEW_NOT_FOUND")) if not state["created"] else []
            ),
        ),
        (
            r"^CREATE TABLE IF NOT EXISTS `_reference_data`.`_catalog`",
            None,
            lambda sql, params: state.__setitem__("created", True) or [],
        ),
        (
            r"^SELECT changed_at",
            ["changed_at", "changed_by", "change_type", "row_id", "before_json", "after_json"],
            audit_rows,
        ),
    ]
    b, conn = make_backend(responses)
    b._write_audit(
        [b._audit_row(form, "r1", "insert", ADMIN, "batch", None, {"code": "x"}, datetime(2024, 1, 1), 0)]
    )
    assert state["created"] and b._audit_ready is True
    assert len(statements(conn, r"^INSERT INTO `_reference_data`.`_catalog`")) == 2
    df = b.get_history(form)
    assert list(df.columns)[:4] == ["version", "changed_at", "changed_by", "change_type"] and df.empty
    # the per-row history adds a bound row filter
    b.get_history(form, limit=50, row_id="r1")
    sql, params = statements(conn, r"^SELECT changed_at")[-1]
    assert "AND row_id = :row_id" in sql and params["row_id"] == "r1" and sql.endswith("LIMIT 50")


# -- authorisation ---------------------------------------------------------------------------


def test_get_permissions_under_user_authorization_uses_current_user_and_group_membership():
    responses = [
        (
            r"SELECT schema_name FROM .*schemata.* WHERE catalog_name = :catalog$",
            ["schema_name"],
            [("finance__cost",), ("hr__reference",), ("information_schema",)],
        ),
        (
            r"schema_privileges",
            ["schema_name", "privilege_type"],
            [("finance__cost", "SELECT"), ("finance__cost", "MODIFY"), ("hr__reference", "SELECT")],
        ),
        (r"catalog_privileges", ["privilege_type"], [("USE CATALOG",)]),
        (r"information_schema`\.`catalogs`", ["catalog_owner"], [("data_platform_admins",)]),
        (r"schema_owner", ["schema_name"], [("hr__reference",)]),
        (r"^SELECT is_account_group_member", ["r"], [(False,)]),
    ]
    b, conn = make_backend(responses)
    perms = b.get_permissions(User("alice@example.org"))
    assert perms.function_roles == {"finance__cost": Role.EDITOR, "hr__reference": Role.ADMIN}
    assert perms.is_global_admin is False
    assert statements(conn, r"is_account_group_member\('data_platform_admins'\)")
    sql = statements(conn, r"schema_privileges")[0][0]
    assert "grantee = current_user() OR is_account_group_member(grantee)" in sql


def test_get_permissions_in_service_principal_mode_uses_resolved_principals():
    responses = [
        (
            r"SELECT schema_name FROM .*schemata.* WHERE catalog_name = :catalog$",
            ["schema_name"],
            [("finance__cost",)],
        ),
        (r"schema_privileges", ["schema_name", "privilege_type"], []),
        (r"catalog_privileges", ["privilege_type"], [("MANAGE",)]),
        (r"information_schema`\.`catalogs`", ["catalog_owner"], [("someone",)]),
        (r"schema_owner", ["schema_name"], []),
    ]
    b, conn = make_backend(responses, token=None)
    perms = b.get_permissions(User("bob@example.org", groups=("finance_readers",)))
    assert perms.function_roles == {"finance__cost": Role.ADMIN} and perms.is_global_admin
    sql, params = statements(conn, r"schema_privileges")[0]
    assert "grantee IN (:g0, :g1)" in sql and set(params.values()) >= {"bob@example.org", "finance_readers"}


@pytest.mark.parametrize(
    ("privs", "role"),
    [
        ({"SELECT"}, Role.VIEWER),
        ({"SELECT", "MODIFY"}, Role.EDITOR),
        ({"SELECT", "MODIFY", "CREATE TABLE"}, Role.ADMIN),
        ({"MANAGE"}, Role.ADMIN),
        ({"ALL PRIVILEGES"}, Role.ADMIN),
        ({"USE SCHEMA"}, Role.NONE),
        (set(), Role.NONE),
    ],
)
def test_role_from_privileges(privs, role):
    assert DatabricksBackend._role_from_privileges(privs) is role


# -- files -------------------------------------------------------------------------------------

VOLUME = "/Volumes/_reference_data/finance__cost/_files"
FILE_REGISTRY_COLS = [
    "name",
    "display_name",
    "description",
    "owner",
    "size_bytes",
    "row_count",
    "created_at",
    "created_by",
    "updated_at",
    "updated_by",
]


def test_put_file_creates_volume_uploads_counts_and_registers():
    responses = [
        (r"^SELECT count\(\*\) FROM read_files", ["c"], [(1200,)]),
        (
            r"SELECT name, display_name, description, owner, size_bytes, row_count",
            FILE_REGISTRY_COLS,
            [("gl.csv", "GL", "d", "o@x", 12, 1200, None, "alice", None, "alice")],
        ),
    ]
    b, conn = make_backend(responses)
    files = b._fake_files
    f = b.put_file(
        FileDef("finance__cost", "gl.csv", display_name="GL", description="d", owner="o@x"),
        b"a,b\n1,2\n",
        ADMIN,
    )
    [(ddl, _)] = statements(conn, r"^CREATE VOLUME IF NOT EXISTS")
    assert ddl.startswith("CREATE VOLUME IF NOT EXISTS `_reference_data`.`finance__cost`.`_files`")
    assert ("upload", VOLUME + "/gl.csv", False) in files.calls  # a new file never overwrites
    [(count_sql, _)] = statements(conn, r"^SELECT count\(\*\) FROM read_files")
    assert count_sql == f"SELECT count(*) FROM read_files('{VOLUME}/gl.csv', format => 'csv', header => true)"
    [(merge, params)] = statements(conn, r"^MERGE INTO `_reference_data`.`_catalog`.`files`")
    assert (
        params["function_name"] == "finance__cost"
        and params["row_count"] == 1200
        and params["size_bytes"] == 8
    )
    [(audit, aparams)] = statements(conn, r"^INSERT INTO `_reference_data`.`_catalog`.`change_log`")
    entry = json.loads(aparams["payload"])[0]
    assert (
        entry["change_type"] == "upload"
        and entry["schema_name"] == "finance__cost"
        and entry["table_name"] == "gl.csv"
    )
    assert json.loads(entry["after_json"]) == {"size_bytes": 8, "row_count": 1200, "format": "csv"}
    assert f.row_count == 1200 and f.display_name == "GL" and f.path == VOLUME + "/gl.csv" and f.registered
    # a second call without replace is a conflict, replace goes through (volume DDL not repeated)
    with pytest.raises(BackendError, match="already exists"):
        b.put_file(FileDef("finance__cost", "gl.csv"), b"x", ADMIN)
    conn.calls.clear()
    b.put_file(FileDef("finance__cost", "gl.csv"), b"a,b\n3,4\n", ADMIN, replace=True)
    assert ("upload", VOLUME + "/gl.csv", True) in files.calls  # replace overwrites
    assert not statements(conn, r"^CREATE VOLUME")
    entry = json.loads(
        statements(conn, r"^INSERT INTO `_reference_data`.`_catalog`.`change_log`")[0][1]["payload"]
    )[0]
    assert entry["change_type"] == "replace" and json.loads(entry["before_json"])["row_count"] == 1200


def test_unreadable_upload_is_removed_again():
    def boom(sql, params):
        raise RuntimeError("MALFORMED_CSV")

    b, conn = make_backend([(r"^SELECT count\(\*\) FROM read_files", None, boom)])
    with pytest.raises(BackendError, match="cannot be read as PARQUET"):
        b.put_file(FileDef("finance__cost", "bad.parquet"), b"garbage", ADMIN)
    assert ("delete", VOLUME + "/bad.parquet") in b._fake_files.calls
    assert b._fake_files.store == {}


def test_list_get_preview_columns_download_and_drop():
    files = FakeFiles()
    files.store[VOLUME + "/gl.csv"] = b"a,b\n1,2\n"
    files.store[VOLUME + "/landed.parquet"] = b"PAR1"
    files.store[VOLUME + "/notes.txt"] = b"ignored"
    responses = [
        (
            r"SELECT name, display_name, description, owner, size_bytes, row_count",
            FILE_REGISTRY_COLS,
            [("gl.csv", "GL", "", "", 8, 1, None, "alice", None, "alice")],
        ),
        (r"^SELECT \* FROM read_files", ["a", "b"], [(1, 2)]),
        (
            r"^DESCRIBE QUERY",
            ["col_name", "data_type", "comment"],
            [("a", "bigint", None), ("b", "string", None)],
        ),
        (
            r"^SELECT changed_at, changed_by, change_type, before_json, after_json FROM",
            ["changed_at", "changed_by", "change_type", "before_json", "after_json"],
            [(None, "alice", "upload", None, '{"size_bytes": 8, "row_count": 1}')],
        ),
    ]
    b, conn = make_backend(responses, files=files)
    listed = {f.name: f for f in b.list_files("finance__cost")}
    assert set(listed) == {"gl.csv", "landed.parquet"}
    assert (
        listed["gl.csv"].registered
        and listed["gl.csv"].row_count == 1
        and listed["gl.csv"].display_name == "GL"
    )
    assert not listed["landed.parquet"].registered and listed["landed.parquet"].size_bytes == 4
    got = b.get_file("finance__cost", "gl.csv")
    assert got.size_bytes == 8 and got.path == VOLUME + "/gl.csv"
    with pytest.raises(NotFoundError):
        b.get_file("finance__cost", "missing.csv")
    preview = b.preview_file(got, limit=5)
    [(sql, _)] = statements(conn, r"^SELECT \* FROM read_files")
    assert sql.endswith("format => 'csv', header => true) LIMIT 5") and preview["a"].tolist() == [1]
    cols = b.file_columns(listed["landed.parquet"])
    [(sql, _)] = statements(conn, r"^DESCRIBE QUERY")
    assert "format => 'parquet')" in sql and [(c.name, c.native_type) for c in cols] == [
        ("a", "BIGINT"),
        ("b", "STRING"),
    ]
    assert b.read_file(got) == b"a,b\n1,2\n"
    history = b.file_history(got)
    [(sql, params)] = statements(conn, r"^SELECT changed_at, changed_by, change_type, before_json")
    assert "change_type IN ('upload', 'replace', 'delete')" in sql and params["table"] == "gl.csv"
    assert history["change_type"].tolist() == ["upload"] and history["row_count"].tolist() == [1]
    b.drop_file(got, ADMIN)
    assert VOLUME + "/gl.csv" not in files.store
    [(sql, params)] = statements(conn, r"^DELETE FROM `_reference_data`.`_catalog`.`files`")
    assert params == {"function_name": "finance__cost", "name": "gl.csv"}
    entry = json.loads(
        statements(conn, r"^INSERT INTO `_reference_data`.`_catalog`.`change_log`")[0][1]["payload"]
    )[0]
    assert entry["change_type"] == "delete" and json.loads(entry["before_json"])["size_bytes"] == 8
    with pytest.raises(NotFoundError):
        b.drop_file(got, ADMIN)


def test_drop_function_refuses_files_then_drops_volume_first():
    files = FakeFiles()
    files.store[VOLUME + "/gl.csv"] = b"x"
    b, conn = make_backend(
        [(r"SELECT count\(\*\) FROM .*information_schema.*tables", ["c"], [(0,)])], files=files
    )
    # the fake listing also returns a sub-folder: everything in the volume counts, not only CSV/Parquet
    with pytest.raises(BackendError, match="still has 2 file\\(s\\) or folder"):
        b.drop_function(FunctionDef("finance__cost"), ADMIN)
    assert not statements(conn, r"^DROP")
    files.store.clear()
    b.drop_function(FunctionDef("finance__cost"), ADMIN)
    sqls = [s for s, _ in conn.calls]
    assert "DROP VOLUME IF EXISTS `_reference_data`.`finance__cost`.`_files`" in sqls
    assert sqls.index("DROP VOLUME IF EXISTS `_reference_data`.`finance__cost`.`_files`") < sqls.index(
        "DROP SCHEMA `_reference_data`.`finance__cost` RESTRICT"
    )


class UnlistableFiles(FakeFiles):
    """Volume exists but cannot be listed (no READ VOLUME, or a Files API outage)."""

    def list_directory_contents(self, path):
        self.calls.append(("list", path))
        raise RuntimeError("PERMISSION_DENIED: User does not have READ VOLUME")


class FolderOnlyFiles(FakeFiles):
    def list_directory_contents(self, path):
        self.calls.append(("list", path))
        return [FakeEntry(path.rstrip("/") + "/archive", is_directory=True)]


def test_drop_function_refuses_when_the_volume_cannot_be_verified_empty():
    tables_empty = [(r"SELECT count\(\*\) FROM .*information_schema.*tables", ["c"], [(0,)])]
    b, conn = make_backend(tables_empty, files=UnlistableFiles())
    with pytest.raises(BackendError, match="Cannot verify that the file volume"):
        b.drop_function(FunctionDef("finance__cost"), ADMIN)
    assert not statements(conn, r"^DROP") and not statements(conn, r"^DELETE")
    # a listing error does not make the function look empty for the UI either
    assert b.list_files("finance__cost") == []
    b, conn = make_backend(tables_empty, files=FolderOnlyFiles())
    with pytest.raises(BackendError, match="still has 1 file\\(s\\) or folder"):
        b.drop_function(FunctionDef("finance__cost"), ADMIN)
    assert not statements(conn, r"^DROP")


def test_new_file_upload_cannot_clobber_an_existing_file():
    class BlindFiles(FakeFiles):
        def get_metadata(self, path):  # the existence check fails for whatever reason
            raise RuntimeError("INTERNAL_ERROR")

    files = BlindFiles()
    files.store[VOLUME + "/gl.csv"] = b"a,b\n1,2\n"
    b, conn = make_backend(files=files)
    with pytest.raises(ConflictError, match="already exists"):
        b.put_file(FileDef("finance__cost", "gl.csv"), b"a,b\n9,9\n", ADMIN)
    assert files.store[VOLUME + "/gl.csv"] == b"a,b\n1,2\n"
    assert not statements(conn, r"^INSERT INTO")


@pytest.mark.parametrize(
    ("statement", "reason"),
    [
        ("SELECT * FROM `other`.`s`.`t`", "outside catalog"),
        ("DELETE FROM `main`.`finance__cost`.`cost_centres`", "outside catalog"),
        ("DROP SCHEMA `_reference_data`.`finance__cost` CASCADE", "CASCADE"),
        ("DROP CATALOG `_reference_data`", "DROP CATALOG"),
        ("TRUNCATE TABLE `_reference_data`.`f`.`t`", "TRUNCATE"),
        ("VACUUM `_reference_data`.`f`.`t`", "VACUUM"),
        ("DELETE FROM `_reference_data`.`f`.`t` WHERE 1 = 1 PURGE", "PURGE"),
        ("USE CATALOG `other`", "USE CATALOG"),
        ("GRANT USE CATALOG ON CATALOG `other` TO `g`", "outside catalog"),
        ("SELECT * FROM table_changes('other.f.t', 0)", "outside catalog"),
        ("SELECT * FROM read_files('/Volumes/other/f/_files/a.csv', format => 'csv')", "outside catalog"),
    ],
)
def test_confinement_guard_refuses_statements_outside_the_catalog(statement, reason):
    b, conn = make_backend()
    with pytest.raises(BackendError, match=reason):
        b._run(statement)
    with pytest.raises(BackendError, match=reason):
        b._run_df(statement)
    assert conn.calls == []  # nothing reached the warehouse


def test_confinement_guard_accepts_the_apps_own_statements():
    b, conn = make_backend()
    for statement in [
        "SELECT * FROM `_reference_data`.`f`.`t`",
        "DROP SCHEMA `_reference_data`.`f` RESTRICT",
        "GRANT USE CATALOG ON CATALOG `_reference_data` TO `g`",
        "SELECT * FROM table_changes('_reference_data.f.t', 0)",
        f"SELECT count(*) FROM read_files('{VOLUME}/a.csv', format => 'csv', header => true)",
        # words inside string literals (comments, descriptions) are data, not clauses
        "COMMENT ON TABLE `_reference_data`.`f`.`t` IS 'cascade of `other`.`a`.`b`; it''s fine'",
        "SELECT current_user(), is_account_group_member('g')",
    ]:
        b._run(statement)
    assert len(conn.calls) == 7


def test_volume_paths_are_confined_to_the_catalog():
    b, _ = make_backend()
    assert b._volume_path("finance__cost") == VOLUME
    assert b._file_path("finance__cost", "gl.csv") == VOLUME + "/gl.csv"
    for bad in [
        "/Volumes/other/finance__cost/_files",
        "/Volumes/_reference_data/finance__cost/data/gl.csv",
        "/Volumes/_reference_data/finance__cost/_files/../gl.csv",
        "/Volumes/_reference_data/finance__cost/_files/sub/gl.csv",
        "/Volumes/_reference_data/_catalog/_files",
        "/Volumes/_reference_data//_files",
        "/tmp/gl.csv",
    ]:
        with pytest.raises((BackendError, ValueError)):
            b._assert_volume_path(bad)
    with pytest.raises(ValueError):
        b._file_path("finance__cost", "../gl.csv")
    with pytest.raises(ValueError):
        b._volume_path("_catalog")


def test_list_functions_counts_registered_files():
    responses = [
        (
            r"schemata.*WHERE catalog_name = :catalog ORDER BY",
            ["schema_name", "comment", "schema_owner"],
            [("finance__cost", "", "alice")],
        ),
        (r"SELECT function_name, count\(\*\) FROM", ["f", "n"], [("finance__cost", 2)]),
    ]
    b, _ = make_backend(responses)
    [f] = b.list_functions()
    assert f.file_count == 2 and f.form_count == 0
