"""SQL generation of the Databricks backend against a fake connection (no warehouse needed)."""

from __future__ import annotations

import json
import re
from datetime import datetime

import pandas as pd
import pytest

from rdm.backend.base import BackendError, NotFoundError
from rdm.backend.databricks_backend import DatabricksBackend
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FormDef,
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


def make_backend(responses=None, token="tok") -> tuple[DatabricksBackend, FakeConnection]:
    conn = FakeConnection(responses)
    b = DatabricksBackend(
        catalog="_forms",
        http_path="/sql/1.0/warehouses/abc",
        access_token=token,
        connection_factory=lambda: conn,
    )
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
    assert b._t(sample_form()) == "`_forms`.`finance__cost`.`cost_centres`"
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
    assert ddl.startswith("CREATE TABLE `_forms`.`finance__cost`.`cost_centres` (")
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


def test_create_domain_issues_create_schema_and_registers_it():
    responses = [
        (
            r"schemata.*WHERE catalog_name = :catalog ORDER BY",
            ["schema_name", "comment", "schema_owner"],
            [("research__rimu", "RIMU lists", "alice")],
        ),
        (
            r"SELECT name, display_name, description, owner, doc_link FROM",
            ["name", "display_name", "description", "owner", "doc_link"],
            [("research__rimu", "Research - RIMU", "RIMU lists", "r@example.org", "https://wiki/rimu")],
        ),
    ]
    b, conn = make_backend(responses)
    domain = b.create_domain(
        DomainDef(
            "research__rimu",
            display_name="Research - RIMU",
            description="RIMU lists",
            owner="r@example.org",
            doc_link="https://wiki/rimu",
        ),
        ADMIN,
    )
    [(ddl, _)] = statements(conn, r"^CREATE SCHEMA")
    assert ddl.startswith("CREATE SCHEMA `_forms`.`research__rimu` COMMENT 'RIMU lists' WITH DBPROPERTIES (")
    assert "'rdm.doc_link' = 'https://wiki/rimu'" in ddl and "'rdm.display_name' = 'Research - RIMU'" in ddl
    assert statements(conn, r"ALTER SCHEMA .* SET TAGS")
    [(merge, params)] = statements(conn, r"^MERGE INTO `_forms`.`_catalog`.`domains`")
    assert params["doc_link"] == "https://wiki/rimu" and params["name"] == "research__rimu"
    assert domain.doc_link == "https://wiki/rimu" and domain.display_name == "Research - RIMU"


def test_grant_domain_role_revokes_then_grants_group_privileges():
    b, conn = make_backend()
    b.grant_domain_role("finance__cost", "finance stewards", Role.EDITOR, ADMIN)
    [(revoke, _)] = statements(conn, r"^REVOKE")
    assert (
        revoke
        == "REVOKE USE SCHEMA, SELECT, MODIFY, CREATE TABLE, MANAGE, APPLY TAG ON SCHEMA `_forms`.`finance__cost` FROM `finance stewards`"
    )
    grants = [s for s, _ in statements(conn, r"^GRANT")]
    assert (
        grants[0]
        == "GRANT USE SCHEMA, SELECT, MODIFY ON SCHEMA `_forms`.`finance__cost` TO `finance stewards`"
    )
    assert grants[1] == "GRANT USE CATALOG ON CATALOG `_forms` TO `finance stewards`"
    conn.calls.clear()
    b.grant_domain_role("finance__cost", "finance_readers", Role.NONE, ADMIN)
    assert len(statements(conn, r"^REVOKE")) == 1 and not statements(conn, r"^GRANT")
    with pytest.raises(BackendError, match="groups only"):
        b.grant_domain_role("finance__cost", "someone@example.org", Role.VIEWER, ADMIN)
    with pytest.raises(BackendError, match="group is required"):
        b.grant_domain_role("finance__cost", " ", Role.VIEWER, ADMIN)


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
    responses = [(r"^SELECT .* FROM `_forms`", [c.name for c in form.columns], [row])]
    b, conn = make_backend(responses)
    df = b.read_rows(form, search="50% of_x", limit=10, order_by="name", descending=True)
    [(sql, params)] = statements(conn, r"^SELECT .* FROM `_forms`")
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
    [(audit, aparams)] = statements(conn, r"^INSERT INTO `_forms`.`_catalog`.`change_log`")
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
    inserts = statements(conn, r"^INSERT INTO `_forms`.`finance__cost`.`cost_centres`")
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
            r"^INSERT INTO `_forms`.`_catalog`",
            None,
            lambda sql, params: (
                (_ for _ in ()).throw(RuntimeError("TABLE_OR_VIEW_NOT_FOUND")) if not state["created"] else []
            ),
        ),
        (
            r"^CREATE TABLE IF NOT EXISTS `_forms`.`_catalog`",
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
    assert len(statements(conn, r"^INSERT INTO `_forms`.`_catalog`")) == 2
    df = b.get_history(form)
    assert list(df.columns)[:4] == ["version", "changed_at", "changed_by", "change_type"] and df.empty


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
    assert perms.domain_roles == {"finance__cost": Role.EDITOR, "hr__reference": Role.ADMIN}
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
    assert perms.domain_roles == {"finance__cost": Role.ADMIN} and perms.is_global_admin
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
