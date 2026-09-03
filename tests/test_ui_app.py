"""The Dash application boots and serves its layout (no browser needed)."""

from __future__ import annotations

import json

import pytest

from rdm.ui import context


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("RDM_BACKEND", "duckdb")
    monkeypatch.setenv("RDM_AUTH", "mock")
    monkeypatch.setenv("RDM_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
    context.get_settings.cache_clear()
    context.get_auth_provider.cache_clear()
    context._backends.clear()
    from rdm.ui.app import create_app

    app = create_app()
    yield app.server.test_client()
    context.get_settings.cache_clear()
    context.get_auth_provider.cache_clear()
    context._backends.clear()


def test_index_and_layout(client):
    assert client.get("/").status_code == 200
    layout = client.get("/_dash-layout").get_json()
    text = json.dumps(layout)
    assert "persona-store" in text and "navbar-content" in text and "page-content" in text


def test_dependencies_list_callbacks(client):
    deps = client.get("/_dash-dependencies").get_json()
    outputs = {d["output"] for d in deps}
    assert any("page-content.children" in o for o in outputs)
    assert any("grid.rowData" in o for o in outputs)
    assert any("draft-store.data" in o for o in outputs)


@pytest.mark.parametrize(
    ("pathname", "persona", "expected"),
    [
        ("/", "admin", "Reference data"),
        ("/domains", "admin", "Business domains group the functions"),
        ("/domains", "editor", "Global admins only"),
        ("/new-function", "admin", "New function"),
        ("/new-file", "viewer", "Function admins only"),
        ("/help", "viewer", "Not designed for"),
        ("/fn/nowhere", "admin", "Access denied"),
        ("/file/finance__cost_management/nothing.csv", "admin", "Access denied"),
        ("/dm/finance", "viewer", "Domain 'finance' does not exist"),  # unseeded database
        ("/dm/ghost", "admin", "Not found"),
    ],
)
def test_page_callback_routes(client, pathname, persona, expected):
    body = {
        "output": "page-content.children",
        "outputs": {"id": "page-content", "property": "children"},
        "inputs": [
            {"id": "url", "property": "pathname", "value": pathname},
            {"id": "persona-store", "property": "data", "value": persona},
            {"id": "nav-version", "property": "data", "value": 0},
        ],
        "changedPropIds": ["url.pathname"],
        "state": [],
    }
    resp = client.post("/_dash-update-component", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200
    assert expected in json.dumps(resp.get_json())
