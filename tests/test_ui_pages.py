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
from rdm.ui.pages import domain_page, form_creator, help_page, home_page


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


@pytest.mark.parametrize("persona", ["admin", "editor", "viewer"])
def test_home_renders_for_every_persona(seeded_backend, persona):
    ctx = make_ctx(seeded_backend, persona)
    tree = home_page.render(ctx)
    assert {ids.HOME_FILTER, ids.HOME_CARDS} <= ids_in(tree)
    text = texts_in(tree)
    assert "Finance - Cost Management" in text if persona != "viewer" else "Finance" not in text
    # the filter narrows the cards
    from rdm.ui.context import navigation

    assert (
        "GL Account Mappings" in texts_in(home_page.cards(navigation(ctx, None), "gl account"))
        if persona != "viewer"
        else True
    )
    assert "No matches" in texts_in(home_page.cards(navigation(ctx, None), "zzz-nothing"))


def test_help_shows_administration_only_to_global_admins(seeded_backend):
    admin_tree = help_page.render(make_ctx(seeded_backend, "admin"))
    editor_tree = help_page.render(make_ctx(seeded_backend, "editor"))
    assert "Administration" in texts_in(admin_tree)
    assert "Administration" not in texts_in(editor_tree)
    assert "Allowed values" in texts_in(admin_tree) and "Allowed values" in texts_in(editor_tree)


def test_domain_page_admin_has_doc_link_group_picker_and_filters(seeded_backend):
    ctx = make_ctx(seeded_backend, "admin")
    tree = domain_page.render(ctx, "finance__cost_management")
    found = ids_in(tree)
    assert {
        ids.DOMAIN_DOC_LINK,
        ids.DOMAIN_FORMS_FILTER,
        ids.GRANTS_FILTER,
        ids.GRANT_PRINCIPAL,
        ids.GRANT_SUBMIT,
    } <= found
    picker = next(n for n in walk(tree) if getattr(n, "id", None) == ids.GRANT_PRINCIPAL)
    assert type(picker).__name__ == "Select" and picker.searchable
    assert {d["value"] for d in picker.data} >= {"finance_readers", "finance_stewards", "rdm_admins"}
    assert "https://wiki.example.org/finance/cost-management" in texts_in(tree) or any(
        getattr(n, "href", None) == "https://wiki.example.org/finance/cost-management" for n in walk(tree)
    )
    assert "Global admin" in texts_in(tree)


def test_domain_page_viewer_has_no_admin_controls(seeded_backend):
    ctx = make_ctx(seeded_backend, "viewer")
    tree = domain_page.render(ctx, "hr__reference")
    found = ids_in(tree)
    assert ids.DOMAIN_FORMS_FILTER in found and ids.GRANT_SUBMIT not in found and ids.DOMAIN_SAVE not in found
    assert "Global admin" not in texts_in(tree)
    denied = domain_page.render(ctx, "finance__cost_management")
    assert "Access denied" in texts_in(denied)


def test_form_cards_filter_and_grants_table_filter(seeded_backend):
    ctx = make_ctx(seeded_backend, "admin")
    forms = seeded_backend.list_forms("finance__cost_management")
    assert "No matches" in texts_in(
        domain_page.form_cards("finance__cost_management", forms, "nothing-here", Role.ADMIN)
    )
    assert "Cost Centres" in texts_in(
        domain_page.form_cards("finance__cost_management", forms, "cost", Role.ADMIN)
    )
    table = domain_page.grants_table(ctx, "finance__cost_management", "stewards")
    assert "finance_stewards" in texts_in(table) and "finance_readers" not in texts_in(table)


def test_new_domain_page_requires_global_admin(seeded_backend):
    assert ids.NEW_DOMAIN_DOC_LINK in ids_in(domain_page.render_new(make_ctx(seeded_backend, "admin")))
    assert "Global admins only" in texts_in(domain_page.render_new(make_ctx(seeded_backend, "editor")))


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
