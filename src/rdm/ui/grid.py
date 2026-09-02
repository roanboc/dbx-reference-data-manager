"""The editable grid (Data tab): toolbar, ``st.data_editor``, pending-changes bar and save.

Editing model
-------------
* Rows are loaded once per *editor version* into a **snapshot** held in session state. The
  frame handed to ``st.data_editor`` is that snapshot minus the technical columns
  (``_id``, ``_version``), so nothing the browser can reveal or edit affects row identity.
* ``st.data_editor`` reports edits positionally; :func:`rdm.services.build_changeset`
  resolves them against the snapshot (same row order) into ``_id`` + ``_version`` pairs.
* Anything that would change the displayed rows (search, sort, refresh, save, discard)
  bumps the version, which gives the editor a new key and a fresh snapshot. While there
  are unsaved edits those controls are locked so positions can never drift.
* The grid lives in a fragment: cell edits re-run only this part of the page.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd
import streamlit as st

from rdm.backend.base import BackendError, PermissionDenied
from rdm.models import (
    AUDIT_COLUMNS,
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    DataType,
    FormDef,
    Role,
    humanize,
)
from rdm.services import EditorState, build_changeset
from rdm.ui import state
from rdm.ui.components import show_error

HIDDEN_COLUMNS = (ID_COLUMN, VERSION_COLUMN)
AUDIT_LABELS = {
    "_created_at": "Created",
    "_created_by": "Created by",
    "_updated_at": "Modified",
    "_updated_by": "Modified by",
}


def column_config(form: FormDef, editable: bool) -> dict[str, Any]:
    """Editor configuration derived from the RDM column types (never from pandas dtypes).

    ``required`` is deliberately *not* set: with ``required=True`` a half-filled new row
    never reaches ``added_rows`` and would be dropped silently on save. Required-ness is
    validated server-side and shown in the pending-changes bar instead.
    """
    cfg: dict[str, Any] = {}
    for c in form.columns:
        if c.name in HIDDEN_COLUMNS:
            continue
        if c.name in AUDIT_COLUMNS:
            label = AUDIT_LABELS.get(c.name, humanize(c.name.lstrip("_")))
            if c.data_type is DataType.TIMESTAMP:
                cfg[c.name] = st.column_config.DatetimeColumn(label, format="YYYY-MM-DD HH:mm", disabled=True)
            else:
                cfg[c.name] = st.column_config.TextColumn(label, disabled=True)
            continue
        label = humanize(c.name)
        flags = [c.type_label] + (["required"] if c.required else []) + (["business key"] if c.is_key else [])
        help_text = (c.description + "\n\n" if c.description else "") + " · ".join(flags)
        t = c.data_type
        if t is DataType.STRING and c.options:
            cfg[c.name] = st.column_config.SelectboxColumn(label, help=help_text, options=c.options)
        elif t is DataType.STRING:
            cfg[c.name] = st.column_config.TextColumn(label, help=help_text)
        elif t is DataType.INTEGER:
            cfg[c.name] = st.column_config.NumberColumn(label, help=help_text, step=1, format="%d")
        elif t is DataType.DECIMAL:
            _p, s = c.decimal_params
            cfg[c.name] = st.column_config.NumberColumn(label, help=help_text, format=f"%.{s}f", step=10**-s)
        elif t is DataType.DOUBLE:
            cfg[c.name] = st.column_config.NumberColumn(label, help=help_text)
        elif t is DataType.BOOLEAN:
            cfg[c.name] = st.column_config.CheckboxColumn(label, help=help_text)
        elif t is DataType.DATE:
            cfg[c.name] = st.column_config.DateColumn(label, help=help_text, format="YYYY-MM-DD")
        elif t is DataType.TIMESTAMP:
            cfg[c.name] = st.column_config.DatetimeColumn(label, help=help_text, format="YYYY-MM-DD HH:mm:ss")
        else:
            cfg[c.name] = st.column_config.Column(
                f"{label} 🔒", help=f"{help_text} · read-only", disabled=True
            )
    return cfg


# --------------------------------------------------------------------------------------
# Snapshot handling
# --------------------------------------------------------------------------------------


def _snapshot_key(full: str) -> str:
    return f"snapshot::{full}::{state.editor_version(full)}"


def _load_snapshot(ctx: state.AppContext, form: FormDef) -> pd.DataFrame:
    key = _snapshot_key(form.full_name)
    if key not in st.session_state:
        st.session_state[key] = ctx.forms.load_rows(
            form,
            search=st.session_state.get(f"grid-search::{form.full_name}") or None,
            limit=ctx.settings.max_rows,
            order_by=_sort_column(form),
            descending=bool(st.session_state.get(f"grid-desc::{form.full_name}", False)),
        )
    return st.session_state[key]


def _sort_column(form: FormDef) -> str | None:
    value = st.session_state.get(f"grid-sort::{form.full_name}")
    return value if value and form.column(value) is not None else None


def _bump(full: str) -> None:
    state.bump_editor_version(full)


def _display_frame(snapshot: pd.DataFrame, show_audit: bool) -> pd.DataFrame:
    drop = [c for c in HIDDEN_COLUMNS if c in snapshot.columns]
    if not show_audit:
        drop += [c for c in AUDIT_COLUMNS if c in snapshot.columns]
    return snapshot.drop(columns=drop)


def _export_frames(form: FormDef, df: pd.DataFrame) -> tuple[bytes, bytes]:
    cols = [c.name for c in form.user_columns if c.name in df.columns]
    export = df[cols].copy()
    csv = export.to_csv(index=False).encode("utf-8-sig")
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        export.to_excel(writer, index=False, sheet_name=(form.name[:31] or "data"))
    return csv, buf.getvalue()


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render(ctx: state.AppContext, form: FormDef, role: Role) -> None:
    editable = role.can_edit and form.is_editable
    if not form.is_editable:
        st.info(
            "This table was not created by the app (it has no `_id`/`_version` columns), so it is shown read-only.",
            icon=":material/info:",
        )
    elif not role.can_edit:
        st.caption(":material/visibility: Read-only - you have Viewer access to this domain.")
    _grid_fragment(ctx, form, editable)


@st.fragment
def _grid_fragment(ctx: state.AppContext, form: FormDef, editable: bool) -> None:
    full = form.full_name
    ekey = state.editor_key(full)
    st.session_state["active_editor_key"] = ekey
    pending = state.has_pending_changes()

    # -- toolbar -----------------------------------------------------------------------------
    t1, t2, t3, t4, t5, t6 = st.columns([3.2, 1.8, 0.9, 1.3, 1.1, 1.3], vertical_alignment="bottom")
    with t1:
        st.text_input(
            "Filter rows",
            key=f"grid-search::{full}",
            placeholder="Search in any column",
            icon=":material/search:",
            disabled=pending,
            label_visibility="collapsed",
            on_change=_bump,
            args=(full,),
            help="Server-side search across all columns. Locked while you have unsaved changes.",
        )
    with t2:
        sortable = [c.name for c in form.user_columns]
        st.selectbox(
            "Sort by",
            [""] + sortable,
            key=f"grid-sort::{full}",
            format_func=lambda v: "Sort: default order" if v == "" else f"Sort: {humanize(v)}",
            disabled=pending,
            label_visibility="collapsed",
            on_change=_bump,
            args=(full,),
        )
    with t3:
        st.toggle(
            "Desc",
            key=f"grid-desc::{full}",
            disabled=pending,
            on_change=_bump,
            args=(full,),
            help="Sort descending",
        )
    with t4:
        show_audit = st.toggle(
            "Audit",
            key=f"grid-audit::{full}",
            disabled=pending,
            on_change=_bump,
            args=(full,),
            help="Show created/modified columns (locked while editing)",
        )
    with t5:
        if st.button(
            "Refresh",
            key=f"grid-refresh::{full}",
            icon=":material/refresh:",
            disabled=pending,
            width="stretch",
        ):
            _bump(full)
            st.rerun()

    try:
        snapshot = _load_snapshot(ctx, form)
    except (BackendError, PermissionDenied) as exc:
        show_error(exc)
        return
    df = _display_frame(snapshot, show_audit)

    with t6, st.popover("Download", icon=":material/download:", width="stretch"):
        csv, xlsx = _export_frames(form, snapshot)
        st.download_button("CSV", csv, file_name=f"{form.name}.csv", mime="text/csv", key=f"dl-csv::{full}")
        st.download_button(
            "Excel",
            xlsx,
            file_name=f"{form.name}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            key=f"dl-xlsx::{full}",
        )

    total = form.row_count if form.row_count is not None else len(snapshot)
    shown = len(snapshot)
    search = st.session_state.get(f"grid-search::{full}") or ""
    if search:
        st.caption(f"{shown:,} matching rows (of {total:,}).")
    elif shown < total:
        st.warning(
            f"Showing the first {shown:,} of {total:,} rows (limit RDM_MAX_ROWS={ctx.settings.max_rows:,}). "
            "Use the filter to find the rows you need.",
            icon=":material/warning:",
        )
    elif shown == 0:
        if editable:
            st.info(
                "This list has no rows yet. Type in the empty row below or use *Import rows*.",
                icon=":material/add_row_below:",
            )
        else:
            st.info("This list has no rows yet.", icon=":material/inbox:")
    else:
        hint = (
            " · click the empty last row to add, select rows and press Delete to remove, paste from Excel works."
            if editable
            else ""
        )
        st.caption(f"{shown:,} rows{hint}")

    # -- grid ----------------------------------------------------------------------------------
    disabled: list[str] | bool
    if not editable:
        disabled = True
    else:
        disabled = [c.name for c in form.columns if c.is_system or c.data_type is DataType.OTHER]
    height = min(36 * (shown + 2) + 6, 620) if shown else 150
    st.data_editor(
        df,
        key=ekey,
        num_rows="dynamic" if editable else "fixed",
        disabled=disabled,
        column_config=column_config(form, editable),
        hide_index=True,
        height=height,
        width="stretch",
        placeholder="",
    )

    if editable:
        _pending_bar(ctx, form, snapshot, ekey)
    _last_result(full)

    # The sidebar locks navigation while there are unsaved edits; it lives outside this
    # fragment, so trigger one full rerun whenever the pending state flips.
    now_pending = state.has_pending_changes()
    flag_key = f"pending-flag::{full}"
    if st.session_state.get(flag_key) != now_pending:
        st.session_state[flag_key] = now_pending
        st.rerun(scope="app")


def _change_details(form: FormDef, snapshot: pd.DataFrame, changes: ChangeSet) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    by_id = snapshot.set_index(ID_COLUMN) if ID_COLUMN in snapshot.columns else None
    for u in changes.updates:
        for col, new in u.changes.items():
            old = by_id.at[u.row_id, col] if by_id is not None and col in by_id.columns else None
            rows.append(
                {
                    "row": u.label,
                    "change": "edit",
                    "column": humanize(col),
                    "from": _fmt(old),
                    "to": _fmt(new),
                }
            )
    for i in changes.inserts:
        summary = ", ".join(f"{humanize(k)}={_fmt(v)}" for k, v in i.values.items() if v is not None)
        rows.append({"row": i.label, "change": "add", "column": "", "from": "", "to": summary[:200]})
    for d in changes.deletes:
        rows.append({"row": d.label, "change": "delete", "column": "", "from": "", "to": ""})
    return pd.DataFrame(rows, columns=["row", "change", "column", "from", "to"])


def _fmt(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value)


def _pending_bar(ctx: state.AppContext, form: FormDef, snapshot: pd.DataFrame, ekey: str) -> None:
    editor_state = EditorState.from_session(st.session_state.get(ekey))
    if editor_state.is_empty:
        return
    changes, issues = build_changeset(form, snapshot, editor_state)
    with st.container(border=True):
        c1, c2, c3 = st.columns([5, 1.2, 1.2], vertical_alignment="center")
        with c1:
            st.markdown(f":material/pending_actions: **Unsaved changes:** {changes.summary()}")
            if issues:
                st.markdown("\n".join(f"- :red[{i}]" for i in issues[:12]))
                if len(issues) > 12:
                    st.caption(f"... and {len(issues) - 12} more.")
            else:
                st.caption("Search, sorting and navigation are locked until you save or discard.")
        with c2:
            save = st.button(
                "Save",
                key=f"save::{ekey}",
                type="primary",
                icon=":material/save:",
                disabled=bool(issues) or changes.is_empty,
                shortcut="Ctrl+S",
                width="stretch",
            )
        with c3:
            discard = st.button("Discard", key=f"discard::{ekey}", icon=":material/undo:", width="stretch")
        with st.expander("Review changes", icon=":material/difference:"):
            st.dataframe(_change_details(form, snapshot, changes), hide_index=True, width="stretch")
    if discard:
        _bump(form.full_name)
        st.rerun()
    if save:
        try:
            result = ctx.forms.save(form, changes)
        except (BackendError, PermissionDenied, ValueError) as exc:
            show_error(exc, "Nothing was saved. ")
            return
        st.session_state[f"last-save::{form.full_name}"] = result
        st.session_state.pop(f"form-def::{form.full_name}", None)
        _bump(form.full_name)
        st.rerun()


def _last_result(full: str) -> None:
    key = f"last-save::{full}"
    result = st.session_state.pop(key, None)
    if result is None:
        return
    if result.applied:
        st.toast(f"Saved: {result.summary()}", icon=":material/check_circle:")
    if result.conflicts:
        st.warning(
            "Some rows were **not** saved because they changed since you loaded them. "
            "The grid has been refreshed; please re-apply those edits:\n\n"
            + "\n".join(f"- {c}" for c in result.conflicts),
            icon=":material/sync_problem:",
        )
    if result.errors:
        st.error("\n".join(result.errors))
