"""Render the Dash pages for each persona without a browser and inspect the component trees."""

from __future__ import annotations

from typing import Any

import pytest

from rdm.auth.provider import PERSONAS, MockAuthProvider
from rdm.config import Settings
from rdm.models import Role
from rdm.services import CatalogService, FormService
from rdm.ui import ids
from rdm.ui.context import AppContext
from rdm.ui.pages import domains_page, file_page, form_creator, function_page, help_page, home_page


def make_ctx(backend, persona: str) -> AppContext:
    user = PERSONAS[persona].user
    perms = backend.get_permissions(user)
    return AppContext(
        settings=Settings(),
        auth=MockAuthProvider(),
        backend=backend,
        user=user,
        permissions=perms,
        catalog=CatalogService(backend, user, perms),
        forms=FormService(backend, user, perms),
    )


def walk(component: Any):
    """Yield every component in a Dash tree."""
    stack = [component]
    while stack:
        node = stack.pop()
        if node is None:
            continue
        if isinstance(node, list | tuple):
            stack.extend(node)
            continue
        yield node
        children = getattr(node, "children", None)
        if children is not None:
            stack.append(children)
        for attr in ("leftSection", "rightSection", "label", "title", "icon"):
            extra = getattr(node, attr, None)
            if extra is not None and not isinstance(extra, str):
                stack.append(extra)


def ids_in(component: Any) -> set[str]:
    return {n.id for n in walk(component) if isinstance(getattr(n, "id", None), str)}


def texts_in(component: Any) -> str:
    parts = []
    for n in walk(component):
        for attr in ("children", "label", "placeholder", "title", "value"):
            v = getattr(n, attr, None)
            if isinstance(v, str):
                parts.append(v)
    return " ".join(parts)


@pytest.mark.parametrize("persona", ["admin", "function_admin", "editor", "viewer"])
def test_home_renders_for_every_persona_grouped_by_domain(seeded_backend, persona):
    ctx = make_ctx(seeded_backend, persona)
    tree = home_page.render(ctx)
    assert {ids.HOME_FILTER, ids.HOME_CARDS} <= ids_in(tree)
    text = texts_in(tree)
    assert "Finance - Cost Management" in text if persona != "viewer" else "Finance" not in text
    assert "Student" in text  # the domain heading of the student function
    assert "People" in text if persona in ("admin", "viewer") else "People" not in text
    # the filter narrows the cards
    from rdm.ui.context import grouped_navigation

    assert (
        "GL Account Mappings" in texts_in(home_page.cards(grouped_navigation(ctx, None), "gl account"))
        if persona != "viewer"
        else True
    )
    assert "No matches" in texts_in(home_page.cards(grouped_navigation(ctx, None), "zzz-nothing"))
    # a domain match keeps every function of the domain
    if persona == "admin":
        assert "Cost Centres" in texts_in(
            home_page.cards(grouped_navigation(ctx, None), "financial planning")
        )


def test_help_shows_administration_only_to_global_admins(seeded_backend):
    admin_tree = help_page.render(make_ctx(seeded_backend, "admin"))
    editor_tree = help_page.render(make_ctx(seeded_backend, "editor"))
    assert "Administration" in texts_in(admin_tree)
    assert "Administration" not in texts_in(editor_tree)
    assert "Allowed values" in texts_in(admin_tree) and "Allowed values" in texts_in(editor_tree)


def test_function_page_admin_has_doc_link_group_picker_and_filters(seeded_backend):
    ctx = make_ctx(seeded_backend, "admin")
    tree = function_page.render(ctx, "finance__cost_management")
    found = ids_in(tree)
    assert {
        ids.FUNCTION_DOC_LINK,
        ids.FUNCTION_FORMS_FILTER,
        ids.FUNCTION_DOMAIN,
        ids.GRANTS_FILTER,
        ids.GRANT_PRINCIPAL,
        ids.GRANT_SUBMIT,
        ids.DROP_FUNCTION_SUBMIT,
    } <= found
    domain_select = next(n for n in walk(tree) if getattr(n, "id", None) == ids.FUNCTION_DOMAIN)
    assert domain_select.value == "finance" and not domain_select.disabled
    drop = next(n for n in walk(tree) if getattr(n, "id", None) == ids.DROP_FUNCTION_SUBMIT)
    assert drop.disabled  # two forms still exist
    picker = next(n for n in walk(tree) if getattr(n, "id", None) == ids.GRANT_PRINCIPAL)
    assert type(picker).__name__ == "Select" and picker.searchable
    assert {d["value"] for d in picker.data} >= {"finance_readers", "finance_stewards", "rdm_admins"}
    assert "https://wiki.example.org/finance/cost-management" in texts_in(tree) or any(
        getattr(n, "href", None) == "https://wiki.example.org/finance/cost-management" for n in walk(tree)
    )
    assert "Global admin" in texts_in(tree)


def test_function_page_function_admin_cannot_delete_or_move_domain(seeded_backend):
    ctx = make_ctx(seeded_backend, "function_admin")
    tree = function_page.render(ctx, "finance__cost_management")
    found = ids_in(tree)
    assert {ids.FUNCTION_SAVE, ids.GRANT_SUBMIT, ids.FUNCTION_DOMAIN} <= found
    assert ids.DROP_FUNCTION_SUBMIT not in found
    assert next(n for n in walk(tree) if getattr(n, "id", None) == ids.FUNCTION_DOMAIN).disabled
    assert "Global admin" not in texts_in(tree)
    # read-only elsewhere
    assert ids.FUNCTION_SAVE not in ids_in(function_page.render(ctx, "student__survey_service_improvement"))


def test_function_page_viewer_has_no_admin_controls(seeded_backend):
    ctx = make_ctx(seeded_backend, "viewer")
    tree = function_page.render(ctx, "hr__reference")
    found = ids_in(tree)
    assert (
        ids.FUNCTION_FORMS_FILTER in found
        and ids.GRANT_SUBMIT not in found
        and ids.FUNCTION_SAVE not in found
    )
    assert "Global admin" not in texts_in(tree)
    assert "People" in texts_in(tree)  # the domain of the function
    denied = function_page.render(ctx, "finance__cost_management")
    assert "Access denied" in texts_in(denied)


def test_form_cards_filter_and_grants_table_filter(seeded_backend):
    ctx = make_ctx(seeded_backend, "admin")
    forms = seeded_backend.list_forms("finance__cost_management")
    assert "No matches" in texts_in(
        function_page.form_cards("finance__cost_management", forms, "nothing-here", Role.ADMIN)
    )
    assert "Cost Centres" in texts_in(
        function_page.form_cards("finance__cost_management", forms, "cost", Role.ADMIN)
    )
    table = function_page.grants_table(ctx, "finance__cost_management", "stewards")
    assert "finance_stewards" in texts_in(table) and "finance_readers" not in texts_in(table)


def test_new_function_page_requires_global_admin(seeded_backend):
    tree = function_page.render_new(make_ctx(seeded_backend, "admin"))
    assert {ids.NEW_FUNCTION_DOC_LINK, ids.NEW_FUNCTION_DOMAIN} <= ids_in(tree)
    domains = next(n for n in walk(tree) if getattr(n, "id", None) == ids.NEW_FUNCTION_DOMAIN)
    assert {d["value"] for d in domains.data} == {"finance", "people", "research", "student"}
    assert "Global admins only" in texts_in(function_page.render_new(make_ctx(seeded_backend, "editor")))
    assert "Global admins only" in texts_in(
        function_page.render_new(make_ctx(seeded_backend, "function_admin"))
    )


def test_domains_page_is_for_global_admins_only(seeded_backend):
    tree = domains_page.render(make_ctx(seeded_backend, "admin"))
    assert {ids.DOMAIN_NAME, ids.DOMAIN_SUBMIT, ids.DOMAINS_TABLE, ids.DOMAIN_DELETE_SUBMIT} <= ids_in(tree)
    text = texts_in(tree)
    assert "Research" in text and "People" in text
    assert "Global admins only" in texts_in(domains_page.render(make_ctx(seeded_backend, "function_admin")))
    assert ids.DOMAIN_SUBMIT not in ids_in(domains_page.render(make_ctx(seeded_backend, "viewer")))
    ctx = make_ctx(seeded_backend, "admin")
    assert "No matching domains" in texts_in(domains_page.domains_table(ctx, "zzz"))
    assert "Finance" in texts_in(domains_page.domains_table(ctx, "fin"))


def test_form_page_settings_delete_zone_only_for_global_admins(seeded_backend):
    from rdm.ui.pages import form_page

    admin_tree = form_page.render(
        make_ctx(seeded_backend, "admin"), "finance__cost_management", "cost_centres"
    )
    fa_tree = form_page.render(
        make_ctx(seeded_backend, "function_admin"), "finance__cost_management", "cost_centres"
    )
    editor_tree = form_page.render(
        make_ctx(seeded_backend, "editor"), "student__survey_service_improvement", "service_areas"
    )
    assert {ids.DROP_FORM_SUBMIT, ids.SETTINGS_SAVE, ids.BULK_OPEN, ids.ITEM_OPEN} <= ids_in(admin_tree)
    assert ids.SETTINGS_SAVE in ids_in(fa_tree) and ids.DROP_FORM_SUBMIT not in ids_in(fa_tree)
    assert "reserved to global administrators" in texts_in(fa_tree)
    assert ids.SETTINGS_SAVE not in ids_in(editor_tree) and ids.BULK_OPEN in ids_in(editor_tree)
    tabs = next(n for n in walk(admin_tree) if getattr(n, "id", None) == ids.FORM_TABS)
    assert tabs.keepMounted is True  # the grid and its draft survive a tab round trip


def test_item_body_lists_fields_and_row_history(seeded_backend):
    from rdm.ui.pages import form_page

    ctx = make_ctx(seeded_backend, "editor")
    form = ctx.forms.get_form("student__survey_service_improvement", "service_areas")
    rows = ctx.forms.load_rows(form)
    row = {k: v for k, v in rows.iloc[0].to_dict().items()}
    history = form_page._history_records(ctx.forms.history(form, row_id=row["_id"]), form)
    body = form_page.item_body(form, row, history, editable=True)
    fields = {
        n.id["column"]
        for n in walk(body)
        if isinstance(getattr(n, "id", None), dict) and n.id.get("type") == "item-field"
    }
    assert fields == {c.name for c in form.user_columns}
    restores = [
        n for n in walk(body) if isinstance(getattr(n, "id", None), dict) and n.id.get("type") == "restore"
    ]
    assert len(restores) == 1 and "History of this row" in texts_in(body)
    read_only = form_page.item_body(form, row, history, editable=False)
    assert not [
        n
        for n in walk(read_only)
        if isinstance(getattr(n, "id", None), dict) and n.id.get("type") == "restore"
    ]
    assert "Read-only view" in texts_in(read_only)


def test_wizard_columns_step_guidance_and_sample_column(seeded_backend):
    state = form_creator._default_state()
    state["mode"] = "scratch"
    state["columns"] = [
        {
            "name": "code",
            "type": "STRING",
            "description": "",
            "required": True,
            "key": True,
            "options": "",
            "samples": "",
            "source": "",
        }
    ]
    tree = form_creator.step_columns(state)
    grid = next(n for n in walk(tree) if getattr(n, "id", None) == ids.WIZ_COLUMNS_GRID)
    samples = next(d for d in grid.columnDefs if d["field"] == "samples")
    assert samples["hide"] is True  # nothing to sample when starting from scratch
    text = texts_in(tree)
    assert "Allowed values" in text and "Active, Inactive, Retired" in text


def test_function_page_lists_files_and_add_file_for_admins(seeded_backend):
    admin_tree = function_page.render(make_ctx(seeded_backend, "admin"), "finance__cost_management")
    assert {ids.FUNCTION_FILES, ids.ADD_FILE_OPEN, ids.ADD_FILE_UPLOAD, ids.ADD_FILE_SUBMIT} <= ids_in(
        admin_tree
    )
    text = texts_in(admin_tree)
    assert "Files (2)" in text and "FX Rates" in text and "GL Transactions (sample)" in text
    editor_tree = function_page.render(make_ctx(seeded_backend, "editor"), "finance__cost_management")
    add = next(n for n in walk(editor_tree) if getattr(n, "id", None) == ids.ADD_FILE_OPEN)
    assert add.style == {"display": "none"}  # viewer on finance
    files = seeded_backend.list_files("finance__cost_management")
    assert "No matches" in texts_in(
        function_page.file_cards("finance__cost_management", files, "zzz", Role.VIEWER)
    )
    assert "FX Rates" in texts_in(
        function_page.file_cards("finance__cost_management", files, "fx", Role.VIEWER)
    )


@pytest.mark.parametrize("persona", ["admin", "function_admin", "editor"])
def test_file_page_renders_per_role(seeded_backend, persona):
    ctx = make_ctx(seeded_backend, persona)
    tree = file_page.render(ctx, "finance__cost_management", "gl_transactions.csv")
    found = ids_in(tree)
    assert {ids.FILE_PREVIEW_GRID, ids.FILE_DOWNLOAD, ids.FILE_REPLACE_OPEN, ids.FILE_TABS} <= found
    text = texts_in(tree)
    assert "2,000 rows" in text and "GL Transactions (sample)" in text
    replace = next(n for n in walk(tree) if getattr(n, "id", None) == ids.FILE_REPLACE_OPEN)
    can_edit = ctx.role_of("finance__cost_management").can_edit
    assert (replace.style == {}) is can_edit
    if persona == "admin":
        assert ids.DROP_FILE_SUBMIT in found and ids.FILE_SETTINGS_SAVE in found
    elif persona == "function_admin":
        assert ids.FILE_SETTINGS_SAVE in found and ids.DROP_FILE_SUBMIT not in found
        assert "reserved to global administrators" in text
    else:
        assert ids.FILE_SETTINGS_SAVE not in found
    file = ctx.forms.get_file("finance__cost_management", "gl_transactions.csv")
    assert "amount_gbp" in texts_in(file_page.columns_panel(ctx, file))
    assert "Uploaded" in texts_in(file_page.history_panel(ctx, file))


def test_file_page_denies_viewer_without_access(seeded_backend):
    from rdm.backend.base import PermissionDenied

    with pytest.raises(PermissionDenied):
        file_page.render(
            make_ctx(seeded_backend, "viewer"), "finance__cost_management", "gl_transactions.csv"
        )


def test_upload_preview_helper(seeded_backend):
    tree = function_page.upload_preview("campus.csv", b"a,b\n1,2\n3,4\n")
    assert "2 columns" in texts_in(tree)
