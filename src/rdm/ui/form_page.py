"""A form: header, then Data / History / Schema / Settings tabs."""

from __future__ import annotations

import copy

import streamlit as st

from rdm.backend.base import BackendError, NotFoundError
from rdm.models import FormDef
from rdm.ui import grid, history, import_export, schema_editor, state
from rdm.ui.components import form_meta_line, role_badge_md, show_error


def load_form(ctx: state.AppContext, domain: str, name: str) -> FormDef:
    key = f"form-def::{domain}.{name}"
    if key not in st.session_state:
        st.session_state[key] = ctx.forms.get_form(domain, name)
    return st.session_state[key]


def render(ctx: state.AppContext, domain: str, name: str) -> None:
    try:
        form = load_form(ctx, domain, name)
    except NotFoundError as exc:
        st.warning(str(exc), icon=":material/search_off:")
        st.button("Back to home", on_click=state.go_home)
        return
    role = ctx.permissions.role_for(domain)
    try:
        domain_def = ctx.backend.get_domain(domain)
        domain_title = domain_def.title
    except BackendError:
        domain_title = domain

    c1, c2 = st.columns([6, 2], vertical_alignment="center")
    with c1:
        st.caption(f":material/folder: {domain_title}  ›  {form.name}")
        st.title(form.title)
        if form.description:
            st.markdown(form.description)
        st.caption(form_meta_line(form) + f" · {role_badge_md(role)}")
    with c2:
        if role.can_edit and form.is_editable:
            import_export.import_button(ctx, form)

    tabs = ["Data", "History", "Schema"]
    if role.can_admin:
        tabs.append("Settings")
    tab_objs = st.tabs(
        [
            f":material/table_rows: {tabs[0]}",
            f":material/history: {tabs[1]}",
            f":material/view_column: {tabs[2]}",
        ]
        + ([":material/settings: Settings"] if role.can_admin else [])
    )
    with tab_objs[0]:
        grid.render(ctx, form, role)
    with tab_objs[1]:
        history.render(ctx, form)
    with tab_objs[2]:
        schema_editor.render(ctx, form, role)
    if role.can_admin:
        with tab_objs[3]:
            _settings(ctx, form)


def _settings(ctx: state.AppContext, form: FormDef) -> None:
    st.subheader("Form details")
    with st.form(key=f"form-settings::{form.full_name}", border=False):
        display_name = st.text_input("Display name", value=form.display_name or form.title)
        description = st.text_area("Description", value=form.description, help="Stored as the table comment")
        owner = st.text_input("Owner", value=form.owner, help="Stored as a table property and tag")
        saved = st.form_submit_button("Save details", type="primary", icon=":material/save:")
    if saved:
        updated = copy.deepcopy(form)
        updated.display_name = display_name.strip()
        updated.description = description.strip()
        updated.owner = owner.strip()
        try:
            new_form = ctx.forms.update_form_metadata(updated)
        except (BackendError, ValueError) as exc:
            show_error(exc)
        else:
            st.session_state[f"form-def::{form.full_name}"] = new_form
            state.invalidate_metadata()
            st.toast("Details saved", icon=":material/check_circle:")
            st.rerun()

    st.subheader("Properties")
    st.caption(
        "Stored as table properties (`TBLPROPERTIES`) and tags in Unity Catalog; in the local `_rdm_meta` store with DuckDB."
    )
    p1, p2 = st.columns(2)
    with p1:
        st.markdown("**Table properties**")
        st.json(form.properties or {}, expanded=False)
    with p2:
        st.markdown("**Tags**")
        st.json(form.tags or {}, expanded=False)

    st.subheader("Danger zone")
    with st.container(border=True):
        st.markdown(
            f"Delete **{form.title}** and all of its {form.row_count or 0:,} rows. This cannot be undone."
        )
        confirm = st.text_input(
            "Type the form name to confirm", key=f"drop-form-confirm::{form.full_name}", placeholder=form.name
        )
        if st.button(
            "Delete form",
            key=f"drop-form::{form.full_name}",
            type="primary",
            icon=":material/delete_forever:",
            disabled=confirm.strip() != form.name,
        ):
            try:
                ctx.forms.drop_form(form)
            except BackendError as exc:
                show_error(exc)
                return
            st.session_state.pop(f"form-def::{form.full_name}", None)
            state.invalidate_metadata()
            st.toast(f"Form '{form.title}' deleted", icon=":material/delete:")
            state.open_domain(form.domain)
            st.rerun()
