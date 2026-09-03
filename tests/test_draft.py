"""Row-identity based drafts (used by the Dash grid)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from rdm.models import ID_COLUMN, VERSION_COLUMN, ColumnDef, DataType, FormDef, system_columns
from rdm.services.draft import (
    Draft,
    build_changeset_from_draft,
    bulk_value,
    invalid_cells,
    is_temp_id,
    json_safe,
    new_temp_id,
    restore_row,
    same_value,
)


def form() -> FormDef:
    return FormDef(
        "dom",
        "frm",
        columns=system_columns()
        + [
            ColumnDef("code", DataType.STRING, nullable=False, is_key=True),
            ColumnDef("name", DataType.STRING),
            ColumnDef("qty", DataType.INTEGER),
            ColumnDef("price", DataType.DECIMAL, precision=10, scale=2),
            ColumnDef("category", DataType.STRING, options=["A", "B"]),
        ],
    )


def rows() -> list[dict]:
    return [
        {
            ID_COLUMN: "r1",
            VERSION_COLUMN: 3,
            "code": "A1",
            "name": "one",
            "qty": 1,
            "price": 1.5,
            "category": "A",
        },
        {
            ID_COLUMN: "r2",
            VERSION_COLUMN: 1,
            "code": "B2",
            "name": "two",
            "qty": None,
            "price": None,
            "category": "B",
        },
    ]


def test_temp_ids():
    tid = new_temp_id()
    assert is_temp_id(tid) and tid.startswith("new:")
    assert not is_temp_id("r1") and not is_temp_id(None)


def test_set_cell_records_version_once_and_round_trips():
    d = Draft()
    d.set_cell("r1", "name", "uno", version=3)
    d.set_cell("r1", "qty", "5", version=99)  # version captured on the first edit only
    assert d.updates == {"r1": {"name": "uno", VERSION_COLUMN: 3, "qty": "5"}}
    again = Draft.from_dict(d.to_dict())
    assert again.updates == d.updates and not again.is_empty
    assert Draft.from_dict(None).is_empty


def test_add_row_edit_and_delete_new_row_removes_it():
    d = Draft()
    tid = d.add_row({"code": "N1"})
    d.set_cell(tid, "name", "new one")
    assert d.inserts == {tid: {"code": "N1", "name": "new one"}}
    d.mark_deleted(tid)
    assert d.is_empty


def test_mark_deleted_drops_pending_updates_for_the_row():
    d = Draft()
    d.set_cell("r1", "name", "x", version=3)
    d.mark_deleted("r1", version=3, label="Row code=A1")
    assert d.updates == {} and d.deletes == {"r1": {VERSION_COLUMN: 3, "label": "Row code=A1"}}


def test_apply_to_rows_overlays_updates_hides_deletes_and_prepends_inserts():
    d = Draft()
    d.set_cell("r1", "name", "uno", version=3)
    d.mark_deleted("r2", version=1)
    tid = d.add_row({"code": "N1"})
    out = d.apply_to_rows(rows(), form())
    assert [r[ID_COLUMN] for r in out] == [tid, "r1"]
    assert out[0]["code"] == "N1" and out[0][VERSION_COLUMN] is None and out[0]["_new"] is True
    assert out[1]["name"] == "uno" and out[1][VERSION_COLUMN] == 3


def test_build_changeset_from_draft_coerces_and_validates():
    d = Draft()
    d.set_cell("r1", "qty", "7", version=3)
    d.set_cell("r1", "price", "2.345", version=3)
    d.set_cell("r2", "category", "Z", version=1)
    tid = d.add_row({"code": "C3", "qty": "x"})
    d.mark_deleted("r2", version=1, label="Row code=B2")
    changes, issues = build_changeset_from_draft(form(), rows(), d)
    # r2 update was dropped when it was marked deleted
    assert [u.row_id for u in changes.updates] == ["r1"]
    upd = changes.updates[0]
    assert (
        upd.changes == {"qty": 7, "price": Decimal("2.35")}
        and upd.expected_version == 3
        and upd.label == "Row code=A1"
    )
    assert changes.inserts[0].values["code"] == "C3" and changes.inserts[0].label == "New row 1"
    assert changes.deletes[0].row_id == "r2" and changes.deletes[0].expected_version == 1
    assert [str(i) for i in issues] == ["New row 1, column 'qty': 'x' is not a valid whole number"]
    assert invalid_cells(issues) == {tid: ["qty"]}


def test_build_changeset_flags_required_options_and_duplicates():
    d = Draft()
    d.set_cell("r1", "code", None, version=3)
    d.set_cell("r1", "category", "nope", version=3)
    tid = d.add_row({"code": "b2"})  # duplicates B2 case-insensitively
    empty = d.add_row({})
    _changes, issues = build_changeset_from_draft(form(), rows(), d)
    messages = [str(i) for i in issues]
    assert "Row code=, column 'code': is required" in messages[0] or any("is required" in m for m in messages)
    assert any("must be one of: A, B" in m for m in messages)
    assert any(m.startswith("New row 1") and "duplicates" in m for m in messages)
    assert any(m.startswith("New row 2") and "is empty" in m for m in messages)
    flagged = invalid_cells(issues)
    assert set(flagged) == {"r1", tid, empty}
    assert flagged[tid] == ["code"] and flagged[empty] == ["code"]  # duplicate key / required


def test_rows_shown_for_pending_inserts_are_not_treated_as_existing():
    d = Draft()
    tid = d.add_row({"code": "N1"})
    shown = d.apply_to_rows(rows(), form())  # includes the temp row
    changes, issues = build_changeset_from_draft(form(), shown, d)
    assert changes.inserts and not issues, issues
    assert tid not in {u.row_id for u in changes.updates}


# --------------------------------------------------------------------------------------
# Bulk update (FR-22) and restore (FR-24) helpers
# --------------------------------------------------------------------------------------


def test_set_many_updates_existing_and_pending_rows():
    d = Draft()
    tid = d.add_row({"code": "N1"})
    touched = d.set_many([*rows(), {ID_COLUMN: tid, VERSION_COLUMN: None}, {"no_id": 1}], "category", "B")
    assert touched == ["r1", "r2", tid]
    assert d.updates == {
        "r1": {"category": "B", VERSION_COLUMN: 3},
        "r2": {"category": "B", VERSION_COLUMN: 1},
    }
    assert d.inserts[tid] == {"code": "N1", "category": "B"}
    changes, issues = build_changeset_from_draft(form(), rows(), d)
    assert not issues and {u.row_id: u.changes for u in changes.updates} == {
        "r1": {"category": "B"},
        "r2": {"category": "B"},
    }


def test_bulk_value_validates_type_options_and_required():
    f = form()
    assert bulk_value(f, "qty", " 12 ") == 12
    assert bulk_value(f, "price", "2.345") == 2.35
    assert bulk_value(f, "category", "A") == "A"
    assert bulk_value(f, "name", None) is None  # optional column can be cleared
    with pytest.raises(ValueError, match="must be one of: A, B"):
        bulk_value(f, "category", "Z")
    with pytest.raises(ValueError, match="not a valid whole number"):
        bulk_value(f, "qty", "abc")
    with pytest.raises(ValueError, match="required and cannot be cleared"):
        bulk_value(f, "code", "")
    with pytest.raises(ValueError, match="Unknown column"):
        bulk_value(f, "ghost", 1)
    with pytest.raises(ValueError, match="Unknown column"):
        bulk_value(f, ID_COLUMN, "x")
    other = FormDef(
        "dom",
        "frm",
        columns=system_columns() + [ColumnDef("blob", DataType.OTHER, native_type="STRUCT<a INT>")],
    )
    with pytest.raises(ValueError, match="read-only"):
        bulk_value(other, "blob", "x")


def test_json_safe_and_same_value():
    from datetime import date, datetime

    assert json_safe(Decimal("1.50")) == 1.5
    assert json_safe(date(2024, 1, 2)) == "2024-01-02"
    assert json_safe(datetime(2024, 1, 2, 3, 4, 5)) == "2024-01-02 03:04:05"
    assert json_safe(None) is None and json_safe(True) is True and json_safe("x") == "x"
    price = ColumnDef("price", DataType.DECIMAL, precision=10, scale=2)
    assert same_value(price, 1.5, "1.50") and not same_value(price, 1.5, "1.51")
    assert same_value(ColumnDef("qty", DataType.INTEGER), None, "") and not same_value(
        ColumnDef("qty", DataType.INTEGER), 1, None
    )
    assert same_value(ColumnDef("name"), "garbage", "garbage")
    assert not same_value(ColumnDef("qty", DataType.INTEGER), "abc", "abd")  # both invalid: compared as text


def test_restore_row_stages_only_the_differences():
    d = Draft()
    current = rows()[0]  # r1: name=one, qty=1, price=1.5, category=A
    snapshot = {"code": "A1", "name": "old name", "qty": "1", "price": "1.50", "category": "B"}
    rid, changed = restore_row(d, form(), current, snapshot)
    assert rid == "r1" and changed == 2
    assert d.updates == {"r1": {"name": "old name", VERSION_COLUMN: 3, "category": "B"}}
    # restoring the same version again changes nothing more
    merged = {**current, **{k: v for k, v in d.updates["r1"].items() if k != VERSION_COLUMN}}
    assert restore_row(d, form(), merged, snapshot) == ("r1", 0)
    # a snapshot value that no longer coerces is kept raw so the validation reports it
    _rid, changed = restore_row(d, form(), merged, {**snapshot, "qty": "lots"})
    assert changed == 1 and d.updates["r1"]["qty"] == "lots"
    assert build_changeset_from_draft(form(), rows(), d)[1]


def test_restore_row_without_current_row_recreates_it_as_a_new_row():
    d = Draft()
    snapshot = {
        "code": "Z9",
        "name": "gone",
        "qty": None,
        "price": "3.00",
        "category": "A",
        "extra": "ignored",
    }
    tid, changed = restore_row(d, form(), None, snapshot)
    assert is_temp_id(tid) and changed == 5
    assert d.inserts[tid] == {"code": "Z9", "name": "gone", "qty": None, "price": 3.0, "category": "A"}
    changes, issues = build_changeset_from_draft(form(), rows(), d)
    assert not issues and changes.inserts[0].values["price"] == Decimal("3.00")
    assert restore_row(d, form(), {ID_COLUMN: None}, snapshot)[0] != tid  # a row without id counts as absent
