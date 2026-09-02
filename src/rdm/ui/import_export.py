"""Bulk row import (Excel/CSV) into an existing form."""

from __future__ import annotations

import streamlit as st

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import FormDef
from rdm.services.excel_import import ImportError_, coerce_frame, list_sheets, map_frame_to_form, read_table
from rdm.ui import state
from rdm.ui.components import show_error


def import_button(ctx: state.AppContext, form: FormDef) -> None:
    if st.button(
        "Import rows",
        key=f"import-open::{form.full_name}",
        icon=":material/upload_file:",
        width="stretch",
        disabled=state.has_pending_changes(),
        help="Append rows from an Excel or CSV file",
    ):
        _import_dialog(ctx, form)


@st.dialog("Import rows", width="large")
def _import_dialog(ctx: state.AppContext, form: FormDef) -> None:
    st.caption(
        f"Rows are **appended** to *{form.title}*. Headers are matched to column names "
        "(spaces and punctuation are ignored). Existing rows are never modified."
    )
    upload = st.file_uploader(
        "Excel or CSV file", type=["xlsx", "xls", "csv", "tsv"], key=f"import-file::{form.full_name}"
    )
    if upload is None:
        return
    data = upload.getvalue()
    try:
        sheets = list_sheets(data, upload.name)
        sheet = (
            st.selectbox("Sheet", sheets, key=f"import-sheet::{form.full_name}")
            if len(sheets) > 1
            else sheets[0]
        )
        raw = read_table(data, upload.name, sheet=sheet if sheet != "data" else None)
    except (ImportError_, ValueError) as exc:
        st.error(str(exc))
        return
    mapping, unmatched = map_frame_to_form(raw, form)
    if not mapping:
        st.error("None of the file's headers match this form's columns.")
        st.write("Expected columns:", ", ".join(c.name for c in form.user_columns))
        return
    if unmatched:
        st.warning(
            "No matching header for: "
            + ", ".join(f"`{c}`" for c in unmatched)
            + " (those values will be empty)."
        )
    extra = [h for h in raw.columns if h not in mapping.values()]
    if extra:
        st.caption("Ignored columns in the file: " + ", ".join(f"`{h}`" for h in extra))
    frame, issues = coerce_frame(raw, form.user_columns, mapping)
    st.dataframe(frame.head(20), hide_index=True, width="stretch")
    st.caption(f"{len(frame):,} rows in the file (showing the first 20).")
    if issues:
        st.error(
            f"{len(issues)} problem(s) found. Fix the file, or import anyway and leave the invalid cells empty."
        )
        st.markdown("\n".join(f"- {i}" for i in issues[:20]))
    label = "Import anyway" if issues else f"Append {len(frame):,} rows"
    if st.button(
        label,
        type="primary",
        icon=":material/upload:",
        key=f"import-go::{form.full_name}",
        disabled=frame.empty,
    ):
        try:
            n = ctx.forms.append_rows(form, frame)
        except (BackendError, PermissionDenied, ValueError) as exc:
            show_error(exc)
            return
        st.session_state.pop(f"form-def::{form.full_name}", None)
        state.bump_editor_version(form.full_name)
        state.invalidate_metadata()
        st.toast(f"Imported {n:,} rows", icon=":material/check_circle:")
        st.rerun()
