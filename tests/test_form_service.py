"""FormService: role guards per persona and end-to-end saves through the draft API."""

from __future__ import annotations

import pandas as pd
import pytest

from rdm.backend.base import BackendError, PermissionDenied
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    RowInsert,
    SaveResult,
    User,
)
from rdm.services.draft import Draft, build_changeset_from_draft
from rdm.services.form_service import FormService

# --------------------------------------------------------------------------------------
# FormService guards (seeded backend)
# --------------------------------------------------------------------------------------

STUDENT = "student__survey_service_improvement"
FINANCE = "finance__cost_management"
HR = "hr__reference"


def service(backend: DuckDBBackend, user: User) -> FormService:
    return FormService(backend, user, backend.get_permissions(user))


def test_service_roles_per_persona(seeded_backend, admin, editor, viewer):
    assert service(seeded_backend, viewer).role(STUDENT) is Role.VIEWER
    assert service(seeded_backend, viewer).role(FINANCE) is Role.NONE
    assert service(seeded_backend, editor).role(STUDENT) is Role.EDITOR
    assert service(seeded_backend, editor).role(FINANCE) is Role.VIEWER
    assert service(seeded_backend, editor).role(HR) is Role.NONE
    assert service(seeded_backend, admin).role(HR) is Role.ADMIN
    assert service(seeded_backend, admin).permissions.can_create_function


def test_viewer_cannot_save(seeded_backend, viewer):
    svc = service(seeded_backend, viewer)
    form = svc.get_form(STUDENT, "service_areas")
    with pytest.raises(
        PermissionDenied,
        match="Editor access to function 'student__survey_service_improvement' is required \\(you have: Viewer\\)",
    ):
        svc.save(form, ChangeSet(inserts=[RowInsert({"area_code": "X"})]))
    with pytest.raises(PermissionDenied):
        svc.save(form, ChangeSet())  # even an empty save needs editor rights
    with pytest.raises(PermissionDenied):
        svc.append_rows(form, pd.DataFrame({"area_code": ["X"]}))
    assert seeded_backend.count_rows(form) == 5


def test_viewer_can_read_visible_functions_only(seeded_backend, viewer):
    svc = service(seeded_backend, viewer)
    form = svc.get_form(STUDENT, "service_areas")
    assert len(svc.load_rows(form)) == 5
    assert len(svc.load_rows(form, search="lib")) == 1
    assert len(svc.load_rows(form, search="", limit=2)) == 2
    assert len(svc.history(form)) == 5
    with pytest.raises(
        PermissionDenied,
        match="Viewer access to function 'finance__cost_management' is required \\(you have: No access\\)",
    ):
        svc.get_form(FINANCE, "cost_centres")
    finance_form = seeded_backend.get_form(FINANCE, "cost_centres")
    with pytest.raises(PermissionDenied):
        svc.load_rows(finance_form)
    with pytest.raises(PermissionDenied):
        svc.history(finance_form)


def test_editor_cannot_administer(seeded_backend, editor):
    svc = service(seeded_backend, editor)
    form = svc.get_form(STUDENT, "service_areas")
    new_form = FormDef(STUDENT, "new_form", columns=[ColumnDef("x")])
    with pytest.raises(PermissionDenied, match="Function admin access to function"):
        svc.create_form(new_form)
    with pytest.raises(PermissionDenied):
        svc.update_form_metadata(form)
    with pytest.raises(PermissionDenied):
        svc.add_column(form, ColumnDef("extra"))
    with pytest.raises(PermissionDenied):
        svc.drop_column(form, "lead_email")
    with pytest.raises(PermissionDenied):
        svc.drop_form(form)
    with pytest.raises(PermissionDenied):
        svc.update_function(FunctionDef(STUDENT, description="x"))
    with pytest.raises(PermissionDenied):
        svc.list_function_grants(STUDENT)
    with pytest.raises(PermissionDenied):
        svc.grant_function_role(STUDENT, "someone", Role.VIEWER)
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_function(FunctionDef("new_function"))
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_domain(DomainDef("new_domain"))
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.delete_domain("student")
    assert [f.name for f in seeded_backend.list_forms(STUDENT)] == ["service_areas", "survey_questions"]
    assert "new_function" not in [d.name for d in seeded_backend.list_functions()]
    assert [d.name for d in svc.list_domains()] == ["finance", "people", "research", "student"]
    assert seeded_backend.get_form(STUDENT, "service_areas").column("lead_email") is not None


def test_function_admin_without_catalog_rights_cannot_create_or_delete(seeded_backend, function_admin):
    svc = service(seeded_backend, function_admin)
    assert svc.role(FINANCE) is Role.ADMIN and not svc.permissions.is_global_admin
    with pytest.raises(PermissionDenied, match="global administrator"):
        svc.create_function(FunctionDef("another"))
    # they administer their function ...
    created = svc.create_form(FormDef(FINANCE, "local_form", columns=[ColumnDef("x")]))
    assert created.owner == function_admin.username
    assert dict(svc.list_function_grants(FINANCE))["finance_admins"] is Role.ADMIN
    assert "finance_admins" in svc.list_groups("finance")
    function = seeded_backend.get_function(FINANCE)
    function.description = "Edited by the function admin"
    assert svc.update_function(function).description == "Edited by the function admin"
    # ... but cannot delete forms or functions, nor move the function to another domain
    with pytest.raises(PermissionDenied, match="Deleting a form requires global administrator"):
        svc.drop_form(created)
    with pytest.raises(PermissionDenied, match="Deleting a function requires global administrator"):
        svc.drop_function(function)
    function.domain = "people"
    with pytest.raises(PermissionDenied, match="Assigning a function to a domain"):
        svc.update_function(function)
    assert seeded_backend.get_function(FINANCE).domain == "finance"
    assert seeded_backend.get_form(FINANCE, "local_form").name == "local_form"


def test_global_admin_can_create_domain_function_and_form_and_delete_them(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    domain = svc.create_domain(DomainDef("library_services", display_name="Library Services"))
    assert domain.owner == admin.username and domain.function_count == 0
    function = svc.create_function(FunctionDef("library", display_name="Library", domain="library_services"))
    assert function.owner == admin.username and function.domain == "library_services"
    assert seeded_backend.get_domain("library_services").function_count == 1
    # permissions were resolved before the function existed; refresh them
    svc = service(seeded_backend, admin)
    form = svc.create_form(
        FormDef("library", "loans", columns=[ColumnDef("loan_id", nullable=False, is_key=True)])
    )
    assert form.row_count == 0
    updated = svc.update_function(
        FunctionDef("library", display_name="Library Services", owner="lib@example.org", domain="student")
    )
    assert updated.display_name == "Library Services" and updated.domain == "student"
    svc.grant_function_role("library", "library_readers", Role.VIEWER)
    assert ("library_readers", Role.VIEWER) in svc.list_function_grants("library")
    with pytest.raises(BackendError, match="still has 1 form"):
        svc.drop_function(updated)
    svc.drop_form(form)
    assert seeded_backend.list_forms("library") == []
    svc.drop_function(updated)
    assert "library" not in [f.name for f in seeded_backend.list_functions()]
    assert svc.update_domain(DomainDef("library_services", display_name="Libraries")).title == "Libraries"
    svc.delete_domain("library_services")
    assert "library_services" not in [d.name for d in svc.list_domains()]
    with pytest.raises(BackendError, match="still has"):
        svc.delete_domain("student")


def _row(rows: list[dict], code: str) -> dict:
    return next(r for r in rows if r["area_code"] == code)


def test_editor_happy_path_save_through_seeded_backend(seeded_backend, editor):
    svc = service(seeded_backend, editor)
    form = svc.get_form(STUDENT, "service_areas")
    rows = svc.load_rows(form).to_dict("records")
    car, est = _row(rows, "CAR"), _row(rows, "EST")
    draft = Draft()
    draft.set_cell(car[ID_COLUMN], "lead_email", "car.lead@example.org", car[VERSION_COLUMN])
    draft.set_cell(car[ID_COLUMN], "target_score", "79", car[VERSION_COLUMN])
    draft.add_row({"area_code": "SPT", "area_name": "Sport", "is_active": "yes", "target_score": "70"})
    draft.mark_deleted(est[ID_COLUMN], est[VERSION_COLUMN])
    changes, issues = build_changeset_from_draft(form, rows, draft)
    assert issues == []
    assert changes.updates[0].label == "Row area_code=CAR"
    result = svc.save(form, changes)
    assert isinstance(result, SaveResult) and not result.conflicts and not result.warnings
    assert (result.inserted, result.updated, result.deleted) == (1, 1, 1)
    after = svc.load_rows(form)
    assert set(after["area_code"]) == {"LIB", "ITS", "WEL", "CAR", "SPT"}
    car = after[after["area_code"] == "CAR"].iloc[0]
    assert car["lead_email"] == "car.lead@example.org" and int(car["target_score"]) == 79
    assert car["_updated_by"] == editor.username
    spt = after[after["area_code"] == "SPT"].iloc[0]
    assert bool(spt["is_active"]) is True and spt["_created_by"] == editor.username
    history = svc.history(form)
    assert history["change_type"].tolist()[:3] == ["delete", "update", "insert"]
    assert history["changed_by"].tolist()[:3] == [editor.username] * 3
    assert history.iloc[0]["area_code"] == "EST"
    assert history.iloc[1]["changed_fields"] == "lead_email, target_score"
    # an empty save is a no-op that touches nothing
    assert svc.save(form, ChangeSet()) == SaveResult()
    assert svc.append_rows(form, pd.DataFrame({"area_code": ["IMP"], "area_name": ["Imported"]})) == 1
    assert seeded_backend.count_rows(form) == 6


def test_editor_stale_edit_is_reported_not_applied(seeded_backend, editor, admin):
    svc = service(seeded_backend, editor)
    form = svc.get_form(STUDENT, "service_areas")
    rows = svc.load_rows(form).to_dict("records")
    lib = _row(rows, "LIB")
    draft = Draft()
    draft.set_cell(lib[ID_COLUMN], "target_score", 90, lib[VERSION_COLUMN])
    changes, _ = build_changeset_from_draft(form, rows, draft)
    # someone else changes the same row in between
    other = service(seeded_backend, admin)
    other_draft = Draft()
    other_draft.set_cell(lib[ID_COLUMN], "target_score", 91, lib[VERSION_COLUMN])
    other_changes, _ = build_changeset_from_draft(form, other.load_rows(form).to_dict("records"), other_draft)
    assert other.save(form, other_changes).updated == 1
    result = svc.save(form, changes)
    assert result.conflicts and result.updated == 0
    assert result.conflicts[0].startswith(f"Row area_code=LIB: modified by {admin.username}")
    row = svc.load_rows(form)
    assert int(row[row["area_code"] == "LIB"].iloc[0]["target_score"]) == 91


def test_service_with_explicit_permissions_object(backend, admin):
    backend.create_function(FunctionDef("dom"), admin)
    svc = FormService(backend, User("anyone"), Permissions({"dom": Role.EDITOR}))
    with pytest.raises(PermissionDenied):
        svc.create_form(FormDef("dom", "frm", columns=[ColumnDef("x")]))
    form = backend.create_form(FormDef("dom", "frm", columns=[ColumnDef("x")]), admin)
    assert svc.save(form, ChangeSet(inserts=[RowInsert({"x": "1"})])).inserted == 1
    assert backend.read_rows(form)["_created_by"].tolist() == ["anyone"]


# --------------------------------------------------------------------------------------
# Files: guards per role
# --------------------------------------------------------------------------------------

CSV = b"code,amount\nA,1\nB,2\n"


def test_file_guards_per_role(seeded_backend, admin, function_admin, editor, viewer):
    # viewer on student: preview / download / history, nothing else
    v = service(seeded_backend, viewer)
    finance_file = seeded_backend.get_file(FINANCE, "gl_transactions.csv")
    with pytest.raises(PermissionDenied):
        v.get_file(FINANCE, "gl_transactions.csv")
    with pytest.raises(PermissionDenied):
        v.preview_file(finance_file)
    # editor has Viewer on finance: can read, cannot replace
    e = service(seeded_backend, editor)
    assert e.get_file(FINANCE, "gl_transactions.csv").row_count == 2000
    assert len(e.preview_file(finance_file, limit=5)) == 5
    assert e.read_file(finance_file)[:10] == b"posting_id"
    assert [c.name for c in e.file_columns(finance_file)][:2] == ["posting_id", "posted_on"]
    assert e.file_history(finance_file)["change_type"].tolist() == ["upload"]
    assert [f.name for f in e.list_files(FINANCE)] == ["fx_rates.parquet", "gl_transactions.csv"]
    with pytest.raises(PermissionDenied, match="Editor access to function 'finance__cost_management'"):
        e.replace_file(finance_file, CSV)
    with pytest.raises(PermissionDenied):
        e.add_file(FileDef(STUDENT, "new.csv"), CSV)  # editor on student, not admin
    # function admin on finance: add, replace, metadata; not delete
    fa = service(seeded_backend, function_admin)
    added = fa.add_file(FileDef(FINANCE, "budget.csv", display_name="Budget"), CSV)
    assert added.row_count == 2 and added.owner == function_admin.username
    replaced = fa.replace_file(added, CSV + b"C,3\n")
    assert replaced.row_count == 3 and replaced.display_name == "Budget"
    replaced.description = "annual budget"
    assert fa.update_file_metadata(replaced).description == "annual budget"
    with pytest.raises(PermissionDenied, match="Deleting a file requires global administrator"):
        fa.drop_file(replaced)
    with pytest.raises(PermissionDenied):
        fa.add_file(FileDef(STUDENT, "x.csv"), CSV)  # only viewer there
    # global admin deletes
    g = service(seeded_backend, admin)
    g.drop_file(replaced)
    assert "budget.csv" not in [f.name for f in seeded_backend.list_files(FINANCE)]
    assert seeded_backend.file_history(replaced)["change_type"].tolist() == ["delete", "replace", "upload"]
