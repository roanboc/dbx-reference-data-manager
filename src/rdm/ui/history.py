"""History tab: row-level change log."""

from __future__ import annotations

import streamlit as st

from rdm.models import ID_COLUMN, FormDef, humanize
from rdm.ui import state
from rdm.ui.components import empty_state, show_error

CHANGE_LABELS = {"insert": "Added", "update": "Edited", "delete": "Deleted"}


def render(ctx: state.AppContext, form: FormDef) -> None:
    c1, c2 = st.columns([3, 1], vertical_alignment="bottom")
    with c1:
        kinds = st.multiselect(
            "Change types",
            list(CHANGE_LABELS),
            default=list(CHANGE_LABELS),
            format_func=CHANGE_LABELS.get,
            key=f"hist-kinds::{form.full_name}",
        )
    with c2:
        limit = st.selectbox("Entries", [100, 250, 500, 1000], key=f"hist-limit::{form.full_name}")
    try:
        df = ctx.forms.history(form, limit=limit)
    except Exception as exc:  # noqa: BLE001
        show_error(exc)
        return
    if df.empty:
        empty_state(
            "No changes yet",
            "Every save is recorded here with who changed what and when.",
            ":material/history:",
        )
        return
    df = df[df["change_type"].isin(kinds)]
    df = df.assign(change_type=df["change_type"].map(CHANGE_LABELS).fillna(df["change_type"]))
    cfg = {
        "version": st.column_config.NumberColumn("Version", format="%d"),
        "changed_at": st.column_config.DatetimeColumn("When", format="YYYY-MM-DD HH:mm:ss"),
        "changed_by": st.column_config.TextColumn("By"),
        "change_type": st.column_config.TextColumn("Change"),
        "changed_fields": st.column_config.TextColumn("Fields changed"),
        ID_COLUMN: st.column_config.TextColumn("Row id", width="small"),
    }
    for c in form.user_columns:
        cfg[c.name] = st.column_config.Column(humanize(c.name))
    st.dataframe(df, column_config=cfg, hide_index=True, width="stretch", height=min(40 * (len(df) + 2), 600))
    st.caption(
        "Edits show the row after the change; deletions show the row as it was. "
        + (
            "In Databricks this view is backed by Delta Change Data Feed and table history."
            if ctx.settings.is_databricks
            else "Locally this is the `_rdm_meta.change_log` table; in Databricks it is Delta Change Data Feed."
        )
    )
