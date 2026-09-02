"""Domain overview and administration (metadata, access grants), plus 'new domain'."""

from __future__ import annotations

import copy

import streamlit as st

from rdm.backend.base import BackendError, NotFoundError
from rdm.models import DomainDef, Role, sanitize_identifier
from rdm.ui import state
from rdm.ui.components import ROLE_COLORS, empty_state, role_badge_md, show_error

GRANTABLE_ROLES = [Role.VIEWER, Role.EDITOR, Role.ADMIN]


def render(ctx: state.AppContext, domain_name: str) -> None:
    role = ctx.permissions.role_for(domain_name)
    if not role.can_view:
        st.warning("You do not have access to this domain.", icon=":material/lock:")
        st.button("Back to home", on_click=state.go_home)
        return
    try:
        domain = ctx.backend.get_domain(domain_name)
    except NotFoundError as exc:
        st.warning(str(exc), icon=":material/search_off:")
        st.button("Back to home", on_click=state.go_home)
        return
    st.caption(f":material/folder: Domain  ›  `{domain.name}`")
    st.title(domain.title)
    if domain.description:
        st.markdown(domain.description)
    st.caption((f"owner {domain.owner} · " if domain.owner else "") + role_badge_md(role))

    forms = ctx.backend.list_forms(domain.name)
    st.subheader(f"Forms ({len(forms)})")
    if not forms:
        empty_state(
            "No forms in this domain", "Administrators can create one from an Excel file with *New form*."
        )
    cols = st.columns(3)
    for i, f in enumerate(forms):
        with cols[i % 3], st.container(border=True):
            st.markdown(f"**{f.title}**")
            st.caption(f.description or "No description")
            meta = []
            if f.row_count is not None:
                meta.append(f"~{f.row_count:,} rows")
            if f.owner:
                meta.append(f"owner {f.owner}")
            if meta:
                st.caption(" · ".join(meta))
            st.button(
                "Open",
                key=f"domain-open-{domain.name}-{f.name}",
                icon=":material/open_in_new:",
                on_click=state.open_form,
                args=(domain.name, f.name),
            )

    if role.can_admin:
        st.divider()
        _admin(ctx, domain)


def _admin(ctx: state.AppContext, domain: DomainDef) -> None:
    st.subheader("Domain settings")
    with st.form(key=f"domain-settings::{domain.name}", border=False):
        display_name = st.text_input("Display name", value=domain.display_name or domain.title)
        description = st.text_area(
            "Description", value=domain.description, help="Stored as the schema comment"
        )
        owner = st.text_input("Owner", value=domain.owner)
        saved = st.form_submit_button("Save", type="primary", icon=":material/save:")
    if saved:
        updated = copy.deepcopy(domain)
        updated.display_name, updated.description, updated.owner = (
            display_name.strip(),
            description.strip(),
            owner.strip(),
        )
        try:
            ctx.forms.update_domain(updated)
        except (BackendError, ValueError) as exc:
            show_error(exc)
        else:
            state.invalidate_metadata()
            st.toast("Domain saved", icon=":material/check_circle:")
            st.rerun()

    st.subheader("Access")
    st.caption(
        "Roles are granted to groups (or individual users) on the whole domain. "
        + (
            "In Databricks these are Unity Catalog schema privileges (SELECT / MODIFY / CREATE TABLE + MANAGE); "
            "manage them in the asset bundle or Catalog Explorer - this view reflects them."
            if ctx.settings.is_databricks
            else "Locally they are stored in the `_rdm_meta.grants` table and emulate Unity Catalog schema privileges."
        )
    )
    try:
        grants = ctx.forms.list_domain_grants(domain.name)
    except BackendError as exc:
        show_error(exc)
        grants = []
    if grants:
        for principal, role in grants:
            c1, c2, c3 = st.columns([3, 2, 1], vertical_alignment="center")
            c1.markdown(f":material/group: **{principal}**")
            c2.markdown(f":{ROLE_COLORS[role]}-badge[{role.label}]")
            if c3.button(
                "Revoke",
                key=f"revoke::{domain.name}::{principal}",
                icon=":material/person_remove:",
                type="tertiary",
                disabled=ctx.settings.is_databricks,
            ):
                try:
                    ctx.forms.grant_domain_role(domain.name, principal, Role.NONE)
                except BackendError as exc:
                    show_error(exc)
                else:
                    state.invalidate_metadata()
                    st.rerun()
    else:
        st.caption("No explicit grants on this domain.")
    if not ctx.settings.is_databricks:
        with st.form(key=f"grant::{domain.name}", clear_on_submit=True, border=False):
            g1, g2, g3 = st.columns([3, 2, 1], vertical_alignment="bottom")
            principal = g1.text_input("Group or user", placeholder="e.g. finance_stewards")
            role = g2.selectbox("Role", GRANTABLE_ROLES, format_func=lambda r: r.label, index=0)
            if g3.form_submit_button("Grant", icon=":material/person_add:") and principal.strip():
                try:
                    ctx.forms.grant_domain_role(domain.name, principal.strip(), role)
                except BackendError as exc:
                    show_error(exc)
                else:
                    state.invalidate_metadata()
                    st.rerun()


def render_new(ctx: state.AppContext) -> None:
    st.title("New domain")
    st.caption("A domain is a Unity Catalog schema that groups the forms of one business function.")
    if not ctx.permissions.can_create_domain:
        st.warning("Creating domains requires catalog administrator rights.", icon=":material/lock:")
        return
    with st.form(key="new-domain", border=True):
        raw = st.text_input(
            "Name",
            placeholder="e.g. student__survey_service_improvement",
            help="Convention: <business_function>__<area>",
        )
        display_name = st.text_input("Display name", placeholder="Student Survey & Service Improvement")
        description = st.text_area("Description")
        owner = st.text_input("Owner", value=ctx.user.email or ctx.user.username)
        submitted = st.form_submit_button(
            "Create domain", type="primary", icon=":material/create_new_folder:"
        )
    if raw:
        st.caption(f"Schema name: `{sanitize_identifier(raw, fallback='domain')}`")
    if submitted:
        name = sanitize_identifier(raw, fallback="")
        if not name:
            st.error("A name is required.")
            return
        try:
            domain = ctx.forms.create_domain(
                DomainDef(name, display_name.strip(), description.strip(), owner.strip())
            )
        except (BackendError, ValueError) as exc:
            show_error(exc)
            return
        state.invalidate_metadata()
        st.toast(f"Domain '{domain.title}' created", icon=":material/check_circle:")
        state.open_domain(domain.name)
        st.rerun()
