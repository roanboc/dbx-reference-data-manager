"""Sidebar: identity, persona switcher (local), search and the domain/form explorer."""

from __future__ import annotations

import streamlit as st

from rdm.config import APP_TITLE
from rdm.models import Role
from rdm.ui import state
from rdm.ui.components import ROLE_COLORS, ROLE_ICONS, empty_state


def _on_persona_change() -> None:
    st.session_state["persona"] = st.session_state["persona_select"]
    st.session_state.pop("_permissions", None)
    state.set_view(state.VIEW_HOME)


def render(ctx: state.AppContext) -> None:
    with st.sidebar:
        st.markdown(f"### :material/table_edit: {APP_TITLE}")
        st.caption(f"Backend: {ctx.backend.describe()}")
        locked = state.has_pending_changes()

        _identity(ctx, locked)
        st.divider()

        search = st.text_input(
            "Search forms",
            key="nav_search",
            placeholder="Search by name or description",
            icon=":material/search:",
            disabled=locked,
            label_visibility="collapsed",
        )
        _explorer(ctx, search, locked)
        st.divider()
        _actions(ctx, locked)


def _identity(ctx: state.AppContext, locked: bool) -> None:
    auth = ctx.auth
    if auth.supports_persona_switching:
        personas = auth.personas()
        keys = [p.key for p in personas]
        current = st.session_state.get("persona", ctx.settings.persona)
        st.selectbox(
            "Signed in as (local persona)",
            keys,
            index=keys.index(current) if current in keys else 0,
            format_func=lambda k: next(p.label for p in personas if p.key == k),
            key="persona_select",
            on_change=_on_persona_change,
            disabled=locked,
            help="Local development only. In Databricks the identity comes from the signed-in user.",
        )
        persona = next(p for p in personas if p.key == current)
        st.caption(persona.description)
        if locked:
            st.caption(":material/lock: Save or discard your changes to switch persona.")
    else:
        st.markdown(f"**{ctx.user.label}**")
        st.caption(ctx.user.email or ctx.user.username)
        if ctx.auth.access_token():
            st.caption(":material/verified_user: Queries run with your own permissions.")
        else:
            st.caption(":material/smart_toy: Queries run as the app service principal.")


def _explorer(ctx: state.AppContext, search: str, locked: bool) -> None:
    try:
        items = state.navigation(ctx, search)
    except Exception as exc:  # noqa: BLE001
        st.error(f"Could not load the catalog: {exc}")
        return
    view = state.current_view()
    if not items:
        if search:
            empty_state("No matches", "Try another word, or clear the search.", ":material/search_off:")
        else:
            empty_state("Nothing to show", "You have not been granted access to any domain yet.")
        return
    for item in items:
        d = item.domain
        is_current_domain = view.domain == d.name
        label = f"{d.title}"
        with st.expander(label, expanded=bool(search) or is_current_domain, icon=ROLE_ICONS[item.role]):
            st.markdown(
                f":{ROLE_COLORS[item.role]}-badge[{item.role.label}]  "
                + (f"{d.form_count} forms" if d.form_count is not None else "")
            )
            st.button(
                "Domain overview",
                key=f"nav-domain-{d.name}",
                icon=":material/folder_open:",
                type="primary" if (is_current_domain and view.view == state.VIEW_DOMAIN) else "tertiary",
                on_click=state.open_domain,
                args=(d.name,),
                disabled=locked,
                width="stretch",
            )
            if not item.forms:
                st.caption(
                    "No forms yet." + (" Use *New form* to create one." if item.role.can_admin else "")
                )
            for f in item.forms:
                selected = is_current_domain and view.form == f.name and view.view == state.VIEW_FORM
                st.button(
                    f.title,
                    key=f"nav-form-{d.name}-{f.name}",
                    help=f.description or None,
                    icon=":material/table:",
                    type="primary" if selected else "tertiary",
                    on_click=state.open_form,
                    args=(d.name, f.name),
                    disabled=locked and not selected,
                    width="stretch",
                )


def _actions(ctx: state.AppContext, locked: bool) -> None:
    perms = ctx.permissions
    if perms.is_admin_anywhere:
        st.button(
            "New form",
            key="nav-new-form",
            icon=":material/add_circle:",
            on_click=state.set_view,
            args=(state.VIEW_NEW_FORM,),
            disabled=locked or not perms.admin_domains,
            help="Create a form from an Excel file or from scratch"
            if perms.admin_domains
            else "You administer no domain yet",
            width="stretch",
        )
    if perms.can_create_domain:
        st.button(
            "New domain",
            key="nav-new-domain",
            icon=":material/create_new_folder:",
            on_click=state.set_view,
            args=(state.VIEW_NEW_DOMAIN,),
            disabled=locked,
            width="stretch",
        )
    st.button(
        "Home",
        key="nav-home",
        icon=":material/home:",
        on_click=state.go_home,
        disabled=locked,
        type="tertiary",
        width="stretch",
    )
    if st.button(
        "Refresh catalog",
        key="nav-refresh",
        icon=":material/refresh:",
        type="tertiary",
        disabled=locked,
        width="stretch",
    ):
        state.invalidate_metadata()
        st.rerun()
    st.caption(
        f"Role legend: {' '.join(f':{ROLE_COLORS[r]}-badge[{r.label}]' for r in (Role.VIEWER, Role.EDITOR, Role.ADMIN))}"
    )
