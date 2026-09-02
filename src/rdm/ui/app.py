"""Application shell: page config, context, sidebar and view routing."""

from __future__ import annotations

import logging

import streamlit as st

from rdm.backend.base import BackendError, PermissionDenied
from rdm.config import APP_ICON, APP_TITLE
from rdm.ui import components, domain_page, form_creator, form_page, home, sidebar, state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


def main() -> None:
    st.set_page_config(
        page_title=APP_TITLE, page_icon=APP_ICON, layout="wide", initial_sidebar_state="expanded"
    )
    components.inject_css()
    try:
        ctx = state.get_context()
    except Exception as exc:  # noqa: BLE001 - configuration problems must be visible
        log.exception("Failed to initialise the application")
        st.error(f"The application could not start: {exc}", icon=":material/error:")
        st.stop()
        return

    sidebar.render(ctx)
    view = state.current_view()
    try:
        if view.view == state.VIEW_FORM and view.domain and view.form:
            form_page.render(ctx, view.domain, view.form)
        elif view.view == state.VIEW_DOMAIN and view.domain:
            domain_page.render(ctx, view.domain)
        elif view.view == state.VIEW_NEW_FORM:
            form_creator.render(ctx)
        elif view.view == state.VIEW_NEW_DOMAIN:
            domain_page.render_new(ctx)
        else:
            home.render(ctx)
    except PermissionDenied as exc:
        st.warning(str(exc), icon=":material/lock:")
        st.button("Back to home", on_click=state.go_home)
    except BackendError as exc:
        components.show_error(exc)
        st.button("Back to home", on_click=state.go_home, key="err-home")
