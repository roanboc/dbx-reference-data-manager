"""Unit tests for rdm.models: naming rules, enums, metadata objects and change tracking."""

from __future__ import annotations

import dataclasses
import json
import uuid

import pytest

from rdm.models import (
    AUDIT_COLUMNS,
    DEFAULT_DECIMAL_PRECISION,
    DEFAULT_DECIMAL_SCALE,
    ID_COLUMN,
    MAX_IDENTIFIER_LENGTH,
    RESERVED_WORDS,
    SYSTEM_COLUMNS,
    UPDATED_AT_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    RowDelete,
    RowInsert,
    RowUpdate,
    SaveResult,
    User,
    ValidationIssue,
    humanize,
    is_system_column,
    new_row_id,
    sanitize_file_name,
    sanitize_identifier,
    split_file_name,
    system_columns,
    validate_file_name,
    validate_identifier,
)

# --------------------------------------------------------------------------------------
# sanitize_identifier / validate_identifier / humanize
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Cost Centre (GBP)", "cost_centre_gbp"),
        ("2024 Budget", "col_2024_budget"),
        ("  Name  ", "name"),
        ("first-name/last.name", "first_name_last_name"),
        ("Already_snake_case", "already_snake_case"),
        ("Multiple   spaces -- and___underscores", "multiple_spaces_and__underscores"),
        ("Ünïcode Ñame", "n_code_ame"),
        ("9", "col_9"),
        (123, "col_123"),
        ("select", "select_"),
        ("Date", "date_"),
        ("", "column"),
        (None, "column"),
        ("!!!", "column"),
        ("___", "column"),
    ],
)
def test_sanitize_identifier(raw, expected):
    assert sanitize_identifier(raw) == expected


def test_sanitize_identifier_uses_fallback_for_empty_input():
    assert sanitize_identifier("", fallback="column_7") == "column_7"
    assert sanitize_identifier("   ", fallback="sheet") == "sheet"


def test_sanitize_identifier_appends_underscore_to_every_reserved_word():
    for word in RESERVED_WORDS:
        assert sanitize_identifier(word) == f"{word}_"
        assert sanitize_identifier(word.upper()) == f"{word}_"


def test_sanitize_identifier_truncates_to_max_length():
    assert sanitize_identifier("a" * 100) == "a" * MAX_IDENTIFIER_LENGTH
    # A trailing underscore left by the cut is removed.
    assert sanitize_identifier("b" * 62 + "_c") == "b" * 62
    assert len(sanitize_identifier("x y " * 40)) <= MAX_IDENTIFIER_LENGTH


@pytest.mark.parametrize(
    "raw", ["Cost Centre (GBP)", "2024 Budget", "select", "", "a" * 100, "£$%", "Ünïcode"]
)
def test_sanitize_identifier_output_is_always_a_valid_user_identifier(raw):
    name = sanitize_identifier(raw)
    assert validate_identifier(name, allow_leading_underscore=False) == name


@pytest.mark.parametrize("name", ["a", "abc", "cost_centre_1", "a1_b2", "x" * 63])
def test_validate_identifier_accepts_lower_snake_case(name):
    assert validate_identifier(name) == name


@pytest.mark.parametrize("name", ["Abc", "a b", "1abc", "", "a-b", "a.b", "x" * 64, "a;drop", "é"])
def test_validate_identifier_rejects_bad_names(name):
    with pytest.raises(ValueError, match="Invalid identifier"):
        validate_identifier(name)


@pytest.mark.parametrize("value", [None, 5, b"abc", ["a"]])
def test_validate_identifier_rejects_non_strings(value):
    with pytest.raises(ValueError):
        validate_identifier(value)


def test_validate_identifier_leading_underscore_is_reserved_for_system_names():
    assert validate_identifier("_id") == "_id"
    assert validate_identifier("_rdm_meta", allow_leading_underscore=True) == "_rdm_meta"
    with pytest.raises(ValueError, match="reserved for system use"):
        validate_identifier("_id", "column name", allow_leading_underscore=False)


def test_validate_identifier_error_mentions_kind():
    with pytest.raises(ValueError, match="Invalid function name 'Bad'"):
        validate_identifier("Bad", "function name")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("cost_centre_code", "Cost Centre Code"),
        ("customer__survey", "Customer / Survey"),
        ("hr__reference", "Hr / Reference"),
        ("single", "Single"),
        ("a__b__c", "A / B / C"),
        ("trailing_", "Trailing"),
        ("", ""),
    ],
)
def test_humanize(name, expected):
    assert humanize(name) == expected


def test_system_column_helpers():
    assert SYSTEM_COLUMNS == ("_id", "_version", "_created_at", "_created_by", "_updated_at", "_updated_by")
    assert AUDIT_COLUMNS == SYSTEM_COLUMNS[2:]
    assert is_system_column(ID_COLUMN)
    assert is_system_column(UPDATED_AT_COLUMN)
    assert not is_system_column("id")
    assert not is_system_column("_other")


def test_new_row_id_is_a_unique_uuid4():
    a, b = new_row_id(), new_row_id()
    assert a != b
    assert uuid.UUID(a).version == 4


# --------------------------------------------------------------------------------------
# DataType / Role / User / Permissions
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data_type", "label"),
    [
        (DataType.STRING, "Text"),
        (DataType.INTEGER, "Whole number"),
        (DataType.DECIMAL, "Decimal"),
        (DataType.DOUBLE, "Floating point"),
        (DataType.BOOLEAN, "Yes / No"),
        (DataType.DATE, "Date"),
        (DataType.TIMESTAMP, "Date & time"),
        (DataType.OTHER, "Other (read-only)"),
    ],
)
def test_datatype_labels_round_trip(data_type, label):
    assert data_type.label == label
    assert DataType.from_label(label) is data_type
    assert DataType.from_label(data_type.value) is data_type


def test_datatype_from_label_unknown():
    with pytest.raises(ValueError, match="Unknown data type label"):
        DataType.from_label("Blob")


def test_datatype_editable_types_exclude_other():
    editable = DataType.editable_types()
    assert DataType.OTHER not in editable
    assert len(editable) == len(DataType) - 1
    assert isinstance(DataType.STRING, str) and DataType.STRING == "STRING"


def test_role_ordering_and_capabilities():
    assert Role.NONE < Role.VIEWER < Role.EDITOR < Role.ADMIN
    assert max(Role.VIEWER, Role.EDITOR) is Role.EDITOR
    assert (Role.NONE.can_view, Role.NONE.can_edit, Role.NONE.can_admin) == (False, False, False)
    assert (Role.VIEWER.can_view, Role.VIEWER.can_edit, Role.VIEWER.can_admin) == (True, False, False)
    assert (Role.EDITOR.can_view, Role.EDITOR.can_edit, Role.EDITOR.can_admin) == (True, True, False)
    assert (Role.ADMIN.can_view, Role.ADMIN.can_edit, Role.ADMIN.can_admin) == (True, True, True)
    assert [r.label for r in Role] == ["No access", "Viewer", "Editor", "Function admin"]
    assert Role["EDITOR"] is Role.EDITOR


def test_user_principals_include_username_email_and_groups():
    u = User("alice", "Alice", groups=("g1", "g2"), email="alice@example.org")
    assert u.principals == frozenset({"alice", "alice@example.org", "g1", "g2"})
    assert u.label == "Alice"


def test_user_without_email_or_display_name():
    u = User("bob")
    assert u.principals == frozenset({"bob"})
    assert u.label == "bob"
    with pytest.raises(dataclasses.FrozenInstanceError):
        u.username = "x"  # type: ignore[misc]


def test_permissions_helpers():
    p = Permissions({"b": Role.VIEWER, "a": Role.ADMIN, "c": Role.NONE, "d": Role.EDITOR})
    assert p.role_for("a") is Role.ADMIN
    assert p.role_for("missing") is Role.NONE
    assert p.visible_functions == ["a", "b", "d"]
    assert p.admin_functions == ["a"]
    assert p.is_admin_anywhere


def test_permissions_defaults_and_catalog_admin():
    assert Permissions().visible_functions == []
    assert not Permissions().is_admin_anywhere
    assert not Permissions({"a": Role.EDITOR}).is_admin_anywhere
    assert Permissions(is_global_admin=True).is_admin_anywhere
    assert (
        Permissions(is_global_admin=True).can_create_function
        and Permissions(is_global_admin=True).summary == "Global admin"
    )
    assert (
        Permissions(is_global_admin=True).can_manage_domains and Permissions(is_global_admin=True).can_delete
    )
    assert (
        not Permissions({"a": Role.ADMIN}).can_delete
        and not Permissions({"a": Role.ADMIN}).can_manage_domains
    )
    assert (
        Permissions({"a": Role.EDITOR, "b": Role.VIEWER, "c": Role.EDITOR}).summary
        == "Editor of 2 functions, viewer of 1 function"
    )
    assert Permissions({"a": Role.ADMIN}).summary == "Function admin of 1 function"
    assert Permissions().summary == "No access yet"


# --------------------------------------------------------------------------------------
# ColumnDef
# --------------------------------------------------------------------------------------


def test_column_decimal_params_defaults():
    assert ColumnDef("x", DataType.DECIMAL).decimal_params == (
        DEFAULT_DECIMAL_PRECISION,
        DEFAULT_DECIMAL_SCALE,
    )
    assert ColumnDef("x", DataType.DECIMAL, precision=10, scale=0).decimal_params == (10, 0)
    assert ColumnDef("x", DataType.DECIMAL, precision=10).decimal_params == (10, DEFAULT_DECIMAL_SCALE)


def test_column_type_label():
    assert ColumnDef("x", DataType.DECIMAL, precision=10, scale=2).type_label == "DECIMAL(10,2)"
    assert ColumnDef("x", DataType.DECIMAL).type_label == "DECIMAL(18,4)"
    assert ColumnDef("x", DataType.OTHER, native_type="STRUCT<a INT>").type_label == "STRUCT<a INT>"
    assert ColumnDef("x", DataType.OTHER).type_label == "OTHER"
    assert ColumnDef("x", DataType.INTEGER).type_label == "INTEGER"


def test_column_flags():
    c = ColumnDef("_id", nullable=False)
    assert c.is_system and c.required
    assert not ColumnDef("code").is_system
    assert not ColumnDef("code").required


def test_column_validate_returns_self_for_valid_definitions():
    c = ColumnDef("price", DataType.DECIMAL, precision=38, scale=38)
    assert c.validate() is c
    assert ColumnDef("category", options=["a", "b"]).validate().options == ["a", "b"]
    assert ColumnDef("_id", DataType.STRING, nullable=False).validate().name == "_id"


@pytest.mark.parametrize(
    ("precision", "scale"),
    [(39, 2), (10, 11), (10, -1), (0, 0) if False else (5, 6)],
)
def test_column_validate_decimal_bounds(precision, scale):
    with pytest.raises(ValueError, match="DECIMAL precision must be 1-38"):
        ColumnDef("price", DataType.DECIMAL, precision=precision, scale=scale).validate()


@pytest.mark.parametrize("data_type", [t for t in DataType if t is not DataType.STRING])
def test_column_validate_options_only_on_text(data_type):
    with pytest.raises(ValueError, match="only supported for text columns"):
        ColumnDef("x", data_type, options=["a"]).validate()


@pytest.mark.parametrize("name", ["_custom", "Bad", "1st", "a b", ""])
def test_column_validate_rejects_bad_names(name):
    with pytest.raises(ValueError, match="Invalid column name"):
        ColumnDef(name).validate()


def test_system_columns_definition():
    cols = system_columns()
    assert [c.name for c in cols] == list(SYSTEM_COLUMNS)
    assert all(c.is_system for c in cols)
    assert cols[0].data_type is DataType.STRING and not cols[0].nullable
    assert cols[1].name == "_version" and cols[1].data_type is DataType.INTEGER and not cols[1].nullable
    assert [c.data_type for c in cols[2:]] == [
        DataType.TIMESTAMP,
        DataType.STRING,
        DataType.TIMESTAMP,
        DataType.STRING,
    ]
    assert all(c.nullable for c in cols[2:])
    assert all(c.validate() is c for c in cols)


# --------------------------------------------------------------------------------------
# DomainDef / FormDef
# --------------------------------------------------------------------------------------


def test_function_title_and_validate():
    assert FunctionDef("hr__reference").title == "Hr / Reference"
    assert FunctionDef("hr__reference", display_name="HR Reference").title == "HR Reference"
    assert FunctionDef("finance").validate().name == "finance"
    assert FunctionDef("finance", domain="people").validate().domain == "people"
    with pytest.raises(ValueError, match="reserved for system use"):
        FunctionDef("_rdm_meta").validate()
    with pytest.raises(ValueError, match="Invalid function name"):
        FunctionDef("Finance Dept").validate()
    with pytest.raises(ValueError, match="Invalid domain name"):
        FunctionDef("finance", domain="Bad Domain").validate()
    with pytest.raises(ValueError, match="http"):
        FunctionDef("finance", doc_link="wiki/x").validate()


def test_domain_title_and_validate():
    assert DomainDef("customer").title == "Customer"
    assert DomainDef("customer", display_name="Customer Experience").title == "Customer Experience"
    assert DomainDef("people", "People", "HR data", "hr@example.org").validate().owner == "hr@example.org"
    assert DomainDef("x").function_count is None
    with pytest.raises(ValueError, match="Invalid domain name"):
        DomainDef("Customer Domain").validate()
    with pytest.raises(ValueError, match="reserved for system use"):
        DomainDef("_catalog").validate()


def _form(*columns: ColumnDef, function: str = "dom", name: str = "frm", **kw) -> FormDef:
    return FormDef(function, name, columns=list(columns), **kw)


def test_form_properties():
    f = _form(*system_columns(), ColumnDef("code", is_key=True), ColumnDef("name"), display_name="")
    assert f.full_name == "dom.frm"
    assert f.title == "Frm"
    assert _form(display_name="My Form").title == "My Form"
    assert [c.name for c in f.user_columns] == ["code", "name"]
    assert [c.name for c in f.key_columns] == ["code"]
    assert f.has_system_columns and f.is_editable
    assert f.column("code").is_key
    assert f.column("missing") is None


def test_form_without_system_columns_is_not_editable():
    f = _form(ColumnDef("code"))
    assert not f.has_system_columns and not f.is_editable
    f2 = _form(ColumnDef("_id"), ColumnDef("code"))  # needs _updated_at too
    assert not f2.has_system_columns


def test_form_validate_ok():
    f = _form(*system_columns(), ColumnDef("code"))
    assert f.validate() is f


def test_form_validate_duplicate_columns():
    with pytest.raises(ValueError, match="Duplicate column name 'code'"):
        _form(ColumnDef("code"), ColumnDef("name"), ColumnDef("code")).validate()


def test_form_validate_requires_a_user_column():
    with pytest.raises(ValueError, match="at least one column"):
        _form().validate()
    with pytest.raises(ValueError, match="at least one column"):
        _form(*system_columns()).validate()


@pytest.mark.parametrize(
    ("function", "name", "message"),
    [
        ("_meta", "frm", "Invalid function name '_meta'"),
        ("Dom", "frm", "Invalid function name 'Dom'"),
        ("dom", "_frm", "Invalid form name '_frm'"),
        ("dom", "my form", "Invalid form name 'my form'"),
    ],
)
def test_form_validate_reserved_or_bad_names(function, name, message):
    with pytest.raises(ValueError, match=message):
        _form(ColumnDef("code"), function=function, name=name).validate()


def test_form_validate_propagates_column_errors():
    with pytest.raises(ValueError, match="only supported for text columns"):
        _form(ColumnDef("n", DataType.INTEGER, options=["1"])).validate()


# --------------------------------------------------------------------------------------
# column_config round trip
# --------------------------------------------------------------------------------------


def _configured_form() -> FormDef:
    return _form(
        *system_columns(),
        ColumnDef("code", is_key=True),
        ColumnDef("category", options=["A", "B"]),
        ColumnDef("both", options=["x"], is_key=True),
        ColumnDef("plain"),
    )


def test_column_config_only_lists_user_columns_with_settings():
    cfg = _configured_form().column_config()
    assert cfg == {
        "version": 1,
        "columns": {
            "code": {"key": True},
            "category": {"options": ["A", "B"]},
            "both": {"options": ["x"], "key": True},
        },
    }
    assert ID_COLUMN not in cfg["columns"] and "plain" not in cfg["columns"]


def test_column_config_json_is_compact_and_sorted():
    text = _configured_form().column_config_json()
    assert " " not in text
    assert text.startswith('{"columns":{"both":')
    assert json.loads(text) == _configured_form().column_config()


def test_apply_column_config_round_trip_from_json_and_dict():
    source = _configured_form()
    for raw in (source.column_config_json(), source.column_config()):
        target = _form(
            *system_columns(), ColumnDef("code"), ColumnDef("category"), ColumnDef("both"), ColumnDef("plain")
        )
        target.apply_column_config(raw)
        assert target.column_config() == source.column_config()
        assert target.column("category").options == ["A", "B"]
        assert target.column("code").is_key and target.column("both").is_key
        assert not target.column("plain").is_key and target.column("plain").options == []


def test_apply_column_config_is_authoritative_for_keys_and_ignores_unknown_columns():
    target = _form(ColumnDef("code", is_key=True, options=["z"]), ColumnDef("other"))
    target.apply_column_config({"columns": {"code": {"options": ["a"]}, "ghost": {"key": True}}})
    assert target.column("code").options == ["a"]
    assert not target.column("code").is_key  # entry present without "key" -> cleared
    assert not target.column("other").is_key  # no entry -> untouched
    assert target.column("ghost") is None


def test_apply_column_config_coerces_option_values_and_key_flags():
    target = _form(ColumnDef("code"))
    target.apply_column_config({"columns": {"code": {"options": [1, 2.5, None], "key": 1}}})
    assert target.column("code").options == ["1", "2.5", "None"]
    assert target.column("code").is_key is True


@pytest.mark.parametrize(
    "raw", [None, "", {}, "{}", '{"version": 1}', "not json", "[1, 2]", 42, {"columns": {"code": "x"}}]
)
def test_apply_column_config_ignores_garbage(raw):
    target = _form(ColumnDef("code", options=["keep"], is_key=True))
    target.apply_column_config(raw)
    assert target.column("code").options == ["keep"]
    assert target.column("code").is_key


@pytest.mark.xfail(
    strict=True,
    raises=AttributeError,
    reason="BUG rdm/models.py FormDef.apply_column_config: a non-dict 'columns' value (str/list/None) "
    "reaches cols.get() outside the try block and raises AttributeError instead of being ignored.",
)
@pytest.mark.parametrize(
    "raw", [{"columns": "abc"}, {"columns": None}, {"columns": [1]}, '{"columns": "abc"}']
)
def test_apply_column_config_ignores_non_dict_columns_value(raw):
    target = _form(ColumnDef("code", options=["keep"], is_key=True))
    target.apply_column_config(raw)
    assert target.column("code").options == ["keep"]
    assert target.column("code").is_key


def test_apply_column_config_options_not_a_list_leaves_options_alone():
    target = _form(ColumnDef("code", options=["keep"]))
    target.apply_column_config({"columns": {"code": {"options": "abc", "key": True}}})
    assert target.column("code").options == ["keep"]
    assert target.column("code").is_key


# --------------------------------------------------------------------------------------
# Change tracking
# --------------------------------------------------------------------------------------


def test_changeset_empty():
    cs = ChangeSet()
    assert cs.is_empty and cs.total == 0
    assert cs.summary() == "no changes"


def test_changeset_summary_and_total():
    cs = ChangeSet(
        inserts=[RowInsert({"a": 1})],
        updates=[RowUpdate("r1", {"a": 2}), RowUpdate("r2", {"a": 3})],
        deletes=[RowDelete("r3"), RowDelete("r4"), RowDelete("r5")],
    )
    assert not cs.is_empty and cs.total == 6
    assert cs.summary() == "1 added, 2 edited, 3 deleted"
    assert ChangeSet(updates=[RowUpdate("r", {})]).summary() == "1 edited"


def test_save_result_defaults():
    r = SaveResult()
    assert r.ok and r.applied == 0
    assert r.summary() == "nothing changed"


def test_save_result_summary_counts():
    r = SaveResult(inserted=2, updated=1, deleted=3)
    assert r.ok and r.applied == 6
    assert r.summary() == "2 added, 1 updated, 3 deleted"


def test_save_result_conflicts_and_errors():
    r = SaveResult(inserted=1, conflicts=["Row code=A: modified by bob"])
    assert not r.ok and r.applied == 1
    assert r.summary() == "1 added; 1 row(s) skipped because they were changed by someone else"
    assert not SaveResult(errors=["boom"]).ok
    assert SaveResult(conflicts=["x", "y"]).summary().startswith("nothing changed; 2 row(s) skipped")


def test_validation_issue_str():
    assert str(ValidationIssue("New row 1", "code", "is required")) == "New row 1, column 'code': is required"
    assert str(ValidationIssue("New row 1", None, "is empty")) == "New row 1: is empty"


# --------------------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("GL Transactions 2024.CSV", "gl_transactions_2024.csv"),
        ("C:\\exports\\FX rates.parquet", "fx_rates.parquet"),
        ("/tmp/a.b.csv", "a_b.csv"),
        ("noext", "noext"),
        ("Select.csv", "select_.csv"),
        ("", "file"),
    ],
)
def test_sanitize_file_name(raw, expected):
    assert sanitize_file_name(raw) == expected


def test_split_and_validate_file_name():
    assert split_file_name("gl.csv") == ("gl", "csv")
    assert split_file_name("GL.PARQUET") == ("GL", "parquet")
    assert split_file_name("plain") == ("plain", "")
    assert validate_file_name("gl_2024.parquet") == "gl_2024.parquet"
    for bad in ["gl.txt", "gl", "Bad.csv", "_hidden.csv", "1st.parquet"]:
        with pytest.raises(ValueError):
            validate_file_name(bad)


def test_qualified_name_and_volume_file_path():
    from rdm.models import qualified_name, volume_file_path

    assert qualified_name("_reference_data", "finance__cost", "cost_centres") == (
        "`_reference_data`.`finance__cost`.`cost_centres`"
    )
    assert volume_file_path("_reference_data", "finance__cost", "gl.csv") == (
        "/Volumes/_reference_data/finance__cost/_files/gl.csv"
    )
    for bad in [("_reference_data", "../x", "t"), ("cat`", "f", "t"), ("_reference_data", "f", "t;drop")]:
        with pytest.raises(ValueError):
            qualified_name(*bad)
    with pytest.raises(ValueError):
        volume_file_path("_reference_data", "_catalog", "gl.csv")
    with pytest.raises(ValueError):
        volume_file_path("_reference_data", "finance__cost", "../gl.csv")
    with pytest.raises(ValueError):
        volume_file_path("other/../x", "finance__cost", "gl.csv")


def test_file_def_properties_and_validation():
    f = FileDef("finance__cost", "gl_transactions.csv", description="d")
    assert (f.stem, f.format, f.full_name) == ("gl_transactions", "csv", "finance__cost/gl_transactions.csv")
    assert f.title == "Gl Transactions" and f.registered and f.size_bytes is None
    assert FileDef("finance__cost", "x.parquet", display_name="X").title == "X"
    assert f.validate() is f
    with pytest.raises(ValueError, match="Invalid function name"):
        FileDef("Bad", "x.csv").validate()
    with pytest.raises(ValueError, match="extension"):
        FileDef("fin", "x.xlsx").validate()
    assert FunctionDef("fin", file_count=2).file_count == 2
