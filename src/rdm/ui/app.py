"""Dash application factory: shell layout, routing and callback registration."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import unquote

import dash
import dash_mantine_components as dmc
from dash import Input, Output, ctx, no_update

from rdm.backend.base import BackendError, PermissionDenied
from rdm.config import APP_TITLE
from rdm.ui import ids, layout
from rdm.ui.components import error_alert
from rdm.ui.context import get_context, navigation
from rdm.ui.pages import domain_page, form_creator, form_page, home_page

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[3] / "assets"


def parse_path(pathname: str | None) -> tuple[str, str | None, str | None]:
    """``/`` -> home, ``/d/<domain>``, ``/f/<domain>/<form>``, ``/new-form``, ``/new-domain``."""
    parts = [unquote(p) for p in (pathname or "/").split("/") if p]
    if not parts:
        return "home", None, None
    if parts[0] == "f" and len(parts) >= 3:
        return "form", parts[1], parts[2]
    if parts[0] == "d" and len(parts) >= 2:
        return "domain", parts[1], None
    if parts[0] == "new-form":
        return "new-form", None, None
    if parts[0] == "new-domain":
        return "new-domain", None, None
    return "home", None, None


def create_app() -> dash.Dash:
    app = dash.Dash(
        __name__,
        title=APP_TITLE,
        external_stylesheets=dmc.styles.ALL,
        suppress_callback_exceptions=True,
        assets_folder=str(ASSETS),
        update_title=None,
    )
    app.layout = layout.shell()
    register_shell_callbacks(app)
    form_page.register(app)
    form_creator.register(app)
    domain_page.register(app)
    return app


def register_shell_callbacks(app: dash.Dash) -> None:
    @app.callback(Output(ids.PERSONA, "data"), Input(ids.PERSONA_SELECT, "value"), prevent_initial_call=True)
    def switch_persona(value):
        return value

    @app.callback(Output("header-content", "children"), Input(ids.PERSONA, "data"))
    def render_header(persona):
        try:
            return layout.header(get_context(persona), persona)
        except Exception as exc:  # noqa: BLE001
            log.exception("header failed")
            return error_alert(exc, "The application could not start")

    @app.callback(
        Output(ids.NAVBAR, "children"),
        Input(ids.URL, "pathname"),
        Input(ids.PERSONA, "data"),
        Input(ids.NAV_SEARCH, "value"),
        Input(ids.NAV_VERSION, "data"),
    )
    def render_navbar(pathname, persona, search, _version):
        try:
            app_ctx = get_context(persona)
            items = navigation(app_ctx, search)
            return layout.navbar(app_ctx, items, pathname or "/", search)
        except Exception as exc:  # noqa: BLE001
            log.exception("navbar failed")
            return error_alert(exc, "Could not load the catalog")

    @app.callback(
        Output(ids.PAGE, "children"),
        Input(ids.URL, "pathname"),
        Input(ids.PERSONA, "data"),
        Input(ids.NAV_VERSION, "data"),
    )
    def render_page(pathname, persona, _version):
        if ctx.triggered_id == ids.NAV_VERSION and parse_path(pathname)[0] == "form":
            return no_update  # form pages manage their own refresh; keep the grid state
        try:
            app_ctx = get_context(persona)
            view, domain, form = parse_path(pathname)
            if view == "form":
                return form_page.render(app_ctx, domain, form)
            if view == "domain":
                return domain_page.render(app_ctx, domain)
            if view == "new-form":
                return form_creator.render(app_ctx)
            if view == "new-domain":
                return domain_page.render_new(app_ctx)
            return home_page.render(app_ctx)
        except PermissionDenied as exc:
            return error_alert(str(exc), "Access denied")
        except BackendError as exc:
            return error_alert(exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("page failed")
            return error_alert(exc)
