"""Landing page: the domains the user can see, with their forms."""

from __future__ import annotations

import streamlit as st

from rdm.ui import state
from rdm.ui.components import ROLE_COLORS, empty_state


def render(ctx: state.AppContext) -> None:
    st.title("Reference data")
    st.caption(
        f"Welcome, {ctx.user.label}. Pick a form from the sidebar or from the domains below. "
        "Forms are editable grids backed by governed tables; every change is recorded."
    )
    items = state.navigation(ctx, st.session_state.get("nav_search"))
    if not items:
        empty_state(
            "No domains available",
            "You have not been granted access to any domain. Ask a domain administrator for Viewer or Editor access.",
        )
        return
    n_forms = sum(len(i.forms) for i in items)
    c1, c2, c3 = st.columns(3)
    c1.metric("Domains", len(items))
    c2.metric("Forms", n_forms)
    c3.metric("You can edit", sum(len(i.forms) for i in items if i.role.can_edit))

    cols = st.columns(2)
    for i, item in enumerate(items):
        d = item.domain
        with cols[i % 2], st.container(border=True):
            st.markdown(f"#### :material/folder: {d.title}")
            st.markdown(
                f":{ROLE_COLORS[item.role]}-badge[{item.role.label}] "
                + (f"· owner {d.owner}" if d.owner else "")
            )
            if d.description:
                st.caption(d.description)
            if not item.forms:
                st.caption("No forms yet.")
            for f in item.forms:
                st.button(
                    f.title,
                    key=f"home-form-{d.name}-{f.name}",
                    help=f.description or None,
                    icon=":material/table:",
                    type="tertiary",
                    on_click=state.open_form,
                    args=(d.name, f.name),
                )
            st.button(
                "Open domain",
                key=f"home-domain-{d.name}",
                icon=":material/folder_open:",
                on_click=state.open_domain,
                args=(d.name,),
            )
