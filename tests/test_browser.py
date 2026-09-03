"""Browser smoke tests (Playwright + Chromium): the parts a server-side test cannot see.

Skipped when no Chromium is available (``playwright install chromium``, or set RDM_TEST_BROWSER
to an existing Chromium executable). The app is served from a thread on a free port with a
freshly seeded DuckDB database, so the tests are self-contained.
"""

from __future__ import annotations

import os
import socket
import threading
from collections.abc import Iterator

import pytest
from werkzeug.serving import make_server

from rdm import demo
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.ui import context

playwright = pytest.importorskip("playwright.sync_api")

COST_CENTRES = "/f/finance__cost_management/cost_centres"
SERVICE_AREAS = "/f/student__survey_service_improvement/service_areas"
FIRST_CHECKBOX = ".ag-pinned-left-cols-container .ag-row >> nth=0 >> .ag-checkbox-input >> nth=0"


@pytest.fixture(scope="module")
def app_url(tmp_path_factory) -> Iterator[str]:
    db = tmp_path_factory.mktemp("browser") / "rdm.duckdb"
    backend = DuckDBBackend(str(db))
    demo.seed(backend)
    backend.close()
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("RDM_BACKEND", "duckdb")
        mp.setenv("RDM_AUTH", "mock")
        mp.setenv("RDM_DUCKDB_PATH", str(db))
        context.get_settings.cache_clear()
        context.get_auth_provider.cache_clear()
        context._backends.clear()
        from rdm.ui.app import create_app

        app = create_app()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = make_server("127.0.0.1", port, app.server, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)
            context.get_settings.cache_clear()
            context.get_auth_provider.cache_clear()
            for _exp, b in context._backends.values():
                b.close()
            context._backends.clear()


@pytest.fixture(scope="module")
def browser() -> Iterator:
    launch = {"executable_path": os.environ["RDM_TEST_BROWSER"]} if os.environ.get("RDM_TEST_BROWSER") else {}
    with playwright.sync_playwright() as p:
        try:
            b = p.chromium.launch(**launch)
        except playwright.Error as exc:  # no browser installed
            pytest.skip(f"Chromium not available for Playwright: {str(exc).splitlines()[0]}")
        try:
            yield b
        finally:
            b.close()


def _page(browser, app_url: str, path: str, color_scheme: str = "light", errors: list | None = None):
    ctx = browser.new_context(viewport={"width": 1400, "height": 900}, color_scheme=color_scheme)
    page = ctx.new_page()
    if errors is not None:
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(app_url + path)
    page.wait_for_selector(".ag-root-wrapper", timeout=30000)
    page.wait_for_timeout(800)
    return page


def _scheme(page) -> dict:
    return page.evaluate(
        """() => { const h = document.documentElement, g = document.querySelector('.ag-root-wrapper');
        return { mantine: h.getAttribute('data-mantine-color-scheme'), ag: h.getAttribute('data-ag-theme-mode'),
                 grid: getComputedStyle(g).backgroundColor, body: getComputedStyle(document.body).backgroundColor,
                 icons: document.querySelectorAll('.rdm-icon').length,
                 mask: (getComputedStyle(document.querySelector('.rdm-icon-search')).maskImage || '') } }"""
    )


def test_colour_scheme_follows_the_system_and_the_header_control(browser, app_url):
    errors: list[str] = []
    page = _page(browser, app_url, COST_CENTRES, color_scheme="dark", errors=errors)
    state = _scheme(page)
    assert state["mantine"] == "dark" and state["ag"] == "dark", state
    assert state["grid"] == state["body"] == "rgb(36, 36, 36)"  # grid and page share the dark background
    assert state["icons"] > 20 and "icons/tabler/search.svg" in state["mask"]  # icons are local SVGs
    control = page.locator("#color-scheme label")
    control.nth(1).click()  # Light
    page.wait_for_timeout(500)
    assert _scheme(page)["mantine"] == "light" and _scheme(page)["grid"] == "rgb(255, 255, 255)"
    control.nth(2).click()  # Dark
    page.wait_for_timeout(500)
    assert _scheme(page)["mantine"] == "dark"
    page.reload()
    page.wait_for_selector(".ag-root-wrapper", timeout=30000)
    page.wait_for_timeout(800)
    assert _scheme(page)["mantine"] == "dark"  # the choice is remembered per browser
    control.nth(0).click()  # System
    page.wait_for_timeout(500)
    assert _scheme(page)["mantine"] == "dark" and _scheme(page)["ag"] == "dark"
    assert not [e for e in errors if "favicon" not in e], errors
    page.context.close()


def test_editor_selects_rows_and_stages_a_delete(browser, app_url):
    page = _page(browser, app_url, COST_CENTRES)
    assert page.locator(".ag-header-select-all").count() == 1  # editors can select all rows
    page.locator(FIRST_CHECKBOX).check(force=True)
    page.wait_for_timeout(300)
    page.click("#grid-delete")
    page.wait_for_timeout(1200)
    assert "1 deleted" in page.locator("#pending-bar").inner_text()
    page.click("#grid-discard")
    page.wait_for_timeout(800)
    assert page.locator("#pending-bar").inner_text().strip() == ""
    page.context.close()


def test_viewer_gets_single_selection_and_a_read_only_item_form(browser, app_url):
    page = _page(browser, app_url, SERVICE_AREAS)
    page.click("#persona-select")
    page.wait_for_timeout(300)
    page.click("text=Vera")
    page.wait_for_timeout(2500)
    page.wait_for_selector(".ag-root-wrapper", timeout=30000)
    assert page.locator(".ag-header-select-all").count() == 0
    assert page.locator(".ag-selection-checkbox").count() >= 1
    page.locator(FIRST_CHECKBOX).check(force=True)
    page.locator(".ag-pinned-left-cols-container .ag-row >> nth=1 >> .ag-checkbox-input >> nth=0").check(
        force=True
    )
    page.wait_for_timeout(300)
    assert (
        page.evaluate("() => document.querySelectorAll('.ag-row-selected').length") == 2
    )  # one row, two panes
    page.click("#item-open")
    page.wait_for_timeout(1200)
    assert page.locator("#item-body input").count() > 0
    assert not page.locator("#item-save").is_visible()
    page.context.close()
