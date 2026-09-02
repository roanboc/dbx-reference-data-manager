"""Schema tab: column definitions; editable by administrators."""

from __future__ import annotations

import copy

import pandas as pd
import streamlit as st

from rdm.backend.base import BackendError
from rdm.models import ColumnDef, DataType, FormDef, Role, humanize, sanitize_identifier
from rdm.ui import state
from rdm.ui.components import TYPE_ICONS, show_error

TYPE_LABELS = {t.value: t.label for t in DataType.editable_types()}


def _columns_frame(form: FormDef) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "name": c.name,
                "type": c.type_label,
                "description": c.description,
                "required": c.required,
                "key": c.is_key,
                "options": ", ".join(c.options),
            }
            for c in form.user_columns
        ]
    )


def render(ctx: state.AppContext, form: FormDef, role: Role) -> None:
    st.caption(
        "Column names and types are fixed once a form exists (changing a type would rewrite the table). "
        "Administrators can edit descriptions, mark columns as required or as part of the business key, "
        "restrict values to a list, add columns and remove columns."
    )
    df = _columns_frame(form)
    if not role.can_admin or not form.is_editable:
        st.dataframe(
            df,
            hide_index=True,
            width="stretch",
            column_config={
                "name": st.column_config.TextColumn("Column"),
                "type": st.column_config.TextColumn("Type"),
                "description": st.column_config.TextColumn("Description", width="large"),
                "required": st.column_config.CheckboxColumn("Required"),
                "key": st.column_config.CheckboxColumn("Business key"),
                "options": st.column_config.TextColumn("Allowed values"),
            },
        )
        _system_columns_note(form)
        return

    version = st.session_state.get(f"schema-version::{form.full_name}", 0)
    edited = st.data_editor(
        df,
        key=f"schema-editor::{form.full_name}::{version}",
        hide_index=True,
        width="stretch",
        disabled=["name", "type"],
        column_config={
            "name": st.column_config.TextColumn("Column", disabled=True),
            "type": st.column_config.TextColumn("Type", disabled=True),
            "description": st.column_config.TextColumn(
                "Description", width="large", help="Shown as a tooltip in the grid"
            ),
            "required": st.column_config.CheckboxColumn(
                "Required", help="Values cannot be empty (existing rows must already comply)"
            ),
            "key": st.column_config.CheckboxColumn(
                "Business key", help="Combination must be unique across rows"
            ),
            "options": st.column_config.TextColumn(
                "Allowed values", help="Comma-separated list; renders as a dropdown (text columns only)"
            ),
        },
    )
    c1, c2, c3 = st.columns([1.2, 1.2, 4])
    with c1:
        if st.button(
            "Save schema",
            key=f"schema-save::{form.full_name}",
            type="primary",
            icon=":material/save:",
            width="stretch",
        ):
            _save(ctx, form, edited, version)
    with c2:
        if st.button("Reset", key=f"schema-reset::{form.full_name}", icon=":material/undo:", width="stretch"):
            st.session_state[f"schema-version::{form.full_name}"] = version + 1
            st.rerun()

    st.divider()
    a1, a2 = st.columns(2)
    with a1:
        _add_column(ctx, form)
    with a2:
        _remove_column(ctx, form)
    _system_columns_note(form)


def _save(ctx: state.AppContext, form: FormDef, edited: pd.DataFrame, version: int) -> None:
    updated = copy.deepcopy(form)
    problems: list[str] = []
    for rec in edited.to_dict("records"):
        col = updated.column(rec["name"])
        if col is None:
            continue
        col.description = (rec.get("description") or "").strip()
        col.nullable = not bool(rec.get("required"))
        col.is_key = bool(rec.get("key"))
        raw_opts = (rec.get("options") or "").strip()
        opts = [o.strip() for o in raw_opts.split(",") if o.strip()] if raw_opts else []
        if opts and col.data_type is not DataType.STRING:
            problems.append(f"'{col.name}': allowed values are only supported for text columns.")
        col.options = list(dict.fromkeys(opts))
    if problems:
        for p in problems:
            st.error(p)
        return
    try:
        new_form = ctx.forms.update_form_metadata(updated)
    except (BackendError, ValueError) as exc:
        show_error(exc)
        return
    st.session_state[f"form-def::{form.full_name}"] = new_form
    st.session_state[f"schema-version::{form.full_name}"] = version + 1
    state.bump_editor_version(form.full_name)
    st.toast("Schema saved", icon=":material/check_circle:")
    st.rerun()


def _add_column(ctx: state.AppContext, form: FormDef) -> None:
    with st.popover("Add column", icon=":material/add:", width="stretch"):
        with st.form(key=f"add-col::{form.full_name}", clear_on_submit=True, border=False):
            raw_name = st.text_input("Column name", placeholder="e.g. Effective date")
            type_value = st.selectbox(
                "Type",
                list(TYPE_LABELS),
                format_func=lambda v: f"{TYPE_LABELS[v]} ({v})",
            )
            description = st.text_input("Description")
            c1, c2 = st.columns(2)
            precision = c1.number_input("Precision", 1, 38, 18, disabled=type_value != "DECIMAL")
            scale = c2.number_input("Scale", 0, 38, 4, disabled=type_value != "DECIMAL")
            submitted = st.form_submit_button("Add", type="primary", icon=":material/add:")
        if submitted:
            name = sanitize_identifier(raw_name)
            if not raw_name.strip():
                st.error("A column name is required.")
                return
            col = ColumnDef(
                name, DataType(type_value), description.strip(), precision=int(precision), scale=int(scale)
            )
            try:
                new_form = ctx.forms.add_column(form, col)
            except (BackendError, ValueError) as exc:
                show_error(exc)
                return
            st.session_state[f"form-def::{form.full_name}"] = new_form
            state.bump_editor_version(form.full_name)
            state.invalidate_metadata()
            st.toast(f"Column '{name}' added", icon=":material/check_circle:")
            st.rerun()


def _remove_column(ctx: state.AppContext, form: FormDef) -> None:
    names = [c.name for c in form.user_columns]
    with st.popover("Remove column", icon=":material/delete:", width="stretch"):
        if len(names) <= 1:
            st.caption("A form needs at least one column.")
            return
        target = st.selectbox(
            "Column",
            names,
            key=f"drop-col-select::{form.full_name}",
            format_func=lambda n: f"{n} ({humanize(n)})",
        )
        st.warning(
            "Removing a column deletes its values in every row. This cannot be undone.",
            icon=":material/warning:",
        )
        confirm = st.text_input("Type the column name to confirm", key=f"drop-col-confirm::{form.full_name}")
        if st.button(
            "Remove column",
            key=f"drop-col::{form.full_name}",
            type="primary",
            icon=":material/delete_forever:",
            disabled=confirm.strip() != target,
        ):
            try:
                new_form = ctx.forms.drop_column(form, target)
            except (BackendError, ValueError) as exc:
                show_error(exc)
                return
            st.session_state[f"form-def::{form.full_name}"] = new_form
            state.bump_editor_version(form.full_name)
            state.invalidate_metadata()
            st.toast(f"Column '{target}' removed", icon=":material/check_circle:")
            st.rerun()


def _system_columns_note(form: FormDef) -> None:
    sys_cols = [c for c in form.columns if c.is_system]
    if sys_cols:
        with st.expander("System columns", icon=":material/settings:"):
            st.markdown(
                "\n".join(
                    f"- {TYPE_ICONS[c.data_type]} `{c.name}` - {c.description or c.type_label}"
                    for c in sys_cols
                )
            )
            st.caption("Managed by the app: row identity, audit trail and concurrency control.")
