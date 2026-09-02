"""Tests for rdm.services.catalog_service on the seeded demo backend."""

from __future__ import annotations

import pytest

from rdm.backend.base import NotFoundError, PermissionDenied
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.models import DomainDef, Permissions, Role, User
from rdm.services.catalog_service import CatalogService, NavDomain

STUDENT = "student__survey_service_improvement"
FINANCE = "finance__cost_management"
HR = "hr__reference"


def service(backend: DuckDBBackend, user: User) -> CatalogService:
    return CatalogService(backend, user, backend.get_permissions(user))


def _nav(items: list[NavDomain]) -> dict[str, tuple[Role, list[str]]]:
    return {i.domain.name: (i.role, [f.name for f in i.forms]) for i in items}


def test_viewer_sees_student_and_hr_only(seeded_backend, viewer):
    nav = service(seeded_backend, viewer).navigation()
    assert [i.domain.name for i in nav] == [HR, STUDENT]  # sorted by name
    assert _nav(nav) == {
        HR: (Role.VIEWER, ["employment_types"]),
        STUDENT: (Role.VIEWER, ["service_areas", "survey_questions"]),
    }
    assert all(isinstance(i.domain, DomainDef) for i in nav)
    assert nav[1].domain.display_name == "Student Survey & Service Improvement"
    assert nav[1].domain.form_count == 2


def test_editor_sees_student_as_editor_and_finance_as_viewer(seeded_backend, editor):
    nav = service(seeded_backend, editor).navigation()
    assert _nav(nav) == {
        FINANCE: (Role.VIEWER, ["cost_centres", "gl_account_mappings"]),
        STUDENT: (Role.EDITOR, ["service_areas", "survey_questions"]),
    }


def test_admin_sees_everything(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    nav = svc.navigation()
    assert _nav(nav) == {
        FINANCE: (Role.ADMIN, ["cost_centres", "gl_account_mappings"]),
        HR: (Role.ADMIN, ["employment_types"]),
        STUDENT: (Role.ADMIN, ["service_areas", "survey_questions"]),
    }
    assert svc.permissions.can_create_domain
    assert [d.domain.name for d in svc.visible_domains()] == [FINANCE, HR, STUDENT]
    assert all(d.forms == [] for d in svc.visible_domains())  # forms are loaded by navigation() only


def test_navigation_forms_are_lightweight_listings(seeded_backend, admin):
    nav = service(seeded_backend, admin).navigation()
    forms = [f for item in nav for f in item.forms]
    assert all(f.columns == [] for f in forms)
    assert {f.display_name for f in forms} >= {"Cost Centres", "Survey Questions", "Employment Types"}


def test_user_without_access_sees_nothing(seeded_backend):
    svc = service(seeded_backend, User("stranger", groups=("everyone",)))
    assert svc.navigation() == []
    assert svc.visible_domains() == []


def test_search_on_domain_title_keeps_all_forms(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    assert _nav(svc.navigation("finance")) == {FINANCE: (Role.ADMIN, ["cost_centres", "gl_account_mappings"])}
    assert _nav(svc.navigation("Cost Management")) == {FINANCE: (Role.ADMIN, ["cost_centres", "gl_account_mappings"])}
    # domain description matches too
    assert _nav(svc.navigation("HR Systems")) == {HR: (Role.ADMIN, ["employment_types"])}
    # matching the schema name works as well
    assert list(_nav(svc.navigation("hr__ref"))) == [HR]


def test_search_matching_a_form_filters_forms(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    assert _nav(svc.navigation("survey_questions")) == {STUDENT: (Role.ADMIN, ["survey_questions"])}
    # form description
    assert _nav(svc.navigation("general ledger")) == {FINANCE: (Role.ADMIN, ["gl_account_mappings"])}
    # form display name, case-insensitive and whitespace-trimmed
    assert _nav(svc.navigation("  COST centres ")) == {FINANCE: (Role.ADMIN, ["cost_centres"])}
    # form owner
    assert _nav(svc.navigation("hr.systems@example.org")) == {HR: (Role.ADMIN, ["employment_types"])}


def test_search_can_hit_several_domains(seeded_backend, admin):
    nav = service(seeded_backend, admin).navigation("reference")
    # "reference" appears in the student and finance domain descriptions and in the hr domain name
    assert set(_nav(nav)) == {STUDENT, FINANCE, HR}
    assert _nav(nav)[FINANCE][1] == ["cost_centres", "gl_account_mappings"]


def test_search_without_match_is_empty(seeded_backend, admin, viewer):
    assert service(seeded_backend, admin).navigation("zzz-nothing") == []
    # the viewer cannot find finance content even though it matches
    assert service(seeded_backend, viewer).navigation("cost centres") == []


@pytest.mark.parametrize("search", [None, "", "   "])
def test_blank_search_returns_everything(seeded_backend, editor, search):
    nav = service(seeded_backend, editor).navigation(search)
    assert set(_nav(nav)) == {STUDENT, FINANCE}


def test_search_results_do_not_mutate_the_full_listing(seeded_backend, admin):
    svc = service(seeded_backend, admin)
    filtered = svc.navigation("survey_questions")
    assert [f.name for f in filtered[0].forms] == ["survey_questions"]
    full = svc.navigation()
    assert _nav(full)[STUDENT][1] == ["service_areas", "survey_questions"]


def test_get_form_respects_visibility(seeded_backend, viewer):
    svc = service(seeded_backend, viewer)
    form = svc.get_form(STUDENT, "survey_questions")
    assert form.row_count == 6 and form.column("question_code").is_key
    with pytest.raises(PermissionDenied, match="You do not have access to domain 'finance__cost_management'"):
        svc.get_form(FINANCE, "cost_centres")
    with pytest.raises(NotFoundError):
        svc.get_form(STUDENT, "no_such_form")


def test_get_form_on_unknown_domain_is_permission_denied(seeded_backend, admin):
    svc = CatalogService(seeded_backend, admin, Permissions({STUDENT: Role.VIEWER}))
    with pytest.raises(PermissionDenied):
        svc.get_form("ghost_domain", "x")
    with pytest.raises(PermissionDenied):
        svc.get_form(HR, "employment_types")


def test_navigation_reflects_new_grants_when_permissions_are_refreshed(seeded_backend, viewer):
    seeded_backend.grant_domain_role(FINANCE, "vera.viewer@example.org", Role.EDITOR, User("seed"))
    stale = CatalogService(seeded_backend, viewer, Permissions({STUDENT: Role.VIEWER}))
    assert set(_nav(stale.navigation())) == {STUDENT}
    fresh = service(seeded_backend, viewer)
    assert _nav(fresh.navigation())[FINANCE][0] is Role.EDITOR
