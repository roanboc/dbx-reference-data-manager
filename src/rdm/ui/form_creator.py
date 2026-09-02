"""Administrator wizard: create a form from an Excel/CSV file or from scratch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd
import streamlit as st

from rdm.backend.base import BackendError
from rdm.models import ColumnDef, DataType, FormDef, humanize, sanitize_identifier
from rdm.services.excel_import import ImportError_, ParsedSheet, coerce_frame, list_sheets, parse_file
from rdm.ui import state
from rdm.ui.components import show_error

STEPS = ["Source", "Columns", "Details", "Review"]
TYPE_OPTIONS = [t.value for t in DataType.editable_types()]
TYPE_LABEL = {t.value: t.label for t in DataType.editable_types()}

EFFECTIVE_DATING_COLUMNS = [
    ColumnDef("valid_from", DataType.DATE, "Start of validity", nullable=False),
    ColumnDef("valid_to", DataType.DATE, "End of validity (empty = current)"),
]


@dataclass
class Wizard:
    step: int = 0
    mode: str = "upload"  # upload | scratch
    file_name: str = ""
    file_bytes: bytes = b""
    sheet: str | None = None
    header_row: int = 0
    parsed: ParsedSheet | None = None
    columns: list[dict[str, Any]] = field(default_factory=list)  # editable rows
    domain: str = ""
    name: str = ""
    display_name: str = ""
    description: str = ""
    owner: str = ""
    load_rows: bool = True
    effective_dating: bool = False
    columns_version: int = 0


def _wizard() -> Wizard:
    return st.session_state.setdefault("wizard", Wizard())


def _reset() -> None:
    st.session_state.pop("wizard", None)


def render(ctx: state.AppContext) -> None:
    st.title("New form")
    if not ctx.permissions.admin_domains:
        st.warning(
            "You need Administrator access to at least one domain to create forms.", icon=":material/lock:"
        )
        return
    w = _wizard()
    st.segmented_control(
        "Step",
        options=list(range(len(STEPS))),
        format_func=lambda i: f"{i + 1}. {STEPS[i]}",
        default=w.step,
        key=f"wizard-step-indicator::{w.step}",
        disabled=True,
        label_visibility="collapsed",
    )
    [_step_source, _step_columns, _step_details, _step_review][w.step](ctx, w)


# -- step 1 ---------------------------------------------------------------------------------


def _step_source(ctx: state.AppContext, w: Wizard) -> None:
    st.subheader("Where does the data come from?")
    mode = st.radio(
        "Source",
        ["upload", "scratch"],
        format_func=lambda m: (
            "Upload an Excel or CSV file" if m == "upload" else "Start from scratch (define columns by hand)"
        ),
        index=0 if w.mode == "upload" else 1,
        label_visibility="collapsed",
    )
    w.mode = mode
    if mode == "upload":
        upload = st.file_uploader("Excel or CSV file", type=["xlsx", "xls", "csv", "tsv"], key="wizard-file")
        if upload is not None:
            data = upload.getvalue()
            try:
                sheets = list_sheets(data, upload.name)
            except Exception as exc:  # noqa: BLE001
                st.error(f"Could not read the file: {exc}")
                return
            c1, c2 = st.columns([2, 1])
            sheet = c1.selectbox("Sheet", sheets, key="wizard-sheet") if len(sheets) > 1 else sheets[0]
            header_row = (
                int(
                    c2.number_input(
                        "Header row (1 = first)", min_value=1, max_value=50, value=1, key="wizard-header"
                    )
                )
                - 1
            )
            try:
                parsed = parse_file(
                    data, upload.name, sheet=None if sheet == "data" else sheet, header_row=header_row
                )
            except (ImportError_, ValueError) as exc:
                st.error(str(exc))
                return
            st.caption(f"{parsed.row_count:,} data rows · {len(parsed.columns)} columns detected")
            st.dataframe(parsed.raw.head(15), hide_index=True, width="stretch")
            for warning in parsed.warnings:
                st.warning(warning)
            if st.button("Next: review columns", type="primary", icon=":material/arrow_forward:"):
                changed = (upload.name, sheet, header_row) != (
                    w.file_name,
                    w.sheet,
                    w.header_row,
                ) or w.parsed is None
                w.file_name, w.file_bytes, w.sheet, w.header_row, w.parsed = (
                    upload.name,
                    data,
                    sheet,
                    header_row,
                    parsed,
                )
                if changed or not w.columns:
                    w.columns = _rows_from_parsed(parsed)
                    w.columns_version += 1
                    base = parsed.sheet if parsed.sheet != "data" else upload.name.rsplit(".", 1)[0]
                    w.name = sanitize_identifier(base, fallback="new_form")
                    w.display_name = humanize(w.name)
                w.step = 1
                st.rerun()
        else:
            st.info(
                "Upload a file to continue. The first sheet's header row becomes the column names; types are inferred from the values.",
                icon=":material/upload_file:",
            )
    else:
        if st.button("Next: define columns", type="primary", icon=":material/arrow_forward:"):
            if w.parsed is not None or not w.columns:
                w.columns = [
                    {
                        "name": "code",
                        "type": "STRING",
                        "description": "Unique code",
                        "required": True,
                        "key": True,
                        "options": "",
                        "samples": "",
                        "source": "",
                    },
                    {
                        "name": "name",
                        "type": "STRING",
                        "description": "",
                        "required": True,
                        "key": False,
                        "options": "",
                        "samples": "",
                        "source": "",
                    },
                ]
                w.columns_version += 1
            w.parsed = None
            w.file_bytes = b""
            w.load_rows = False
            w.step = 1
            st.rerun()


def _rows_from_parsed(parsed: ParsedSheet) -> list[dict[str, Any]]:
    rows = []
    for c in parsed.columns:
        rows.append(
            {
                "name": c.name,
                "type": c.data_type.value,
                "description": "",
                "required": False,
                "key": c.name in parsed.suggested_keys,
                "options": ", ".join(parsed.suggested_options.get(c.name, [])),
                "samples": ", ".join(parsed.samples.get(c.name, [])),
                "source": parsed.source_names.get(c.name, ""),
            }
        )
    return rows


# -- step 2 ---------------------------------------------------------------------------------


def _step_columns(ctx: state.AppContext, w: Wizard) -> None:
    st.subheader("Confirm the columns")
    st.caption(
        "Adjust names (lower_snake_case), types and descriptions. Mark columns that must always have a value as *required*, "
        "and the column(s) that identify a row as *business key*. *Allowed values* turns a text column into a dropdown."
    )
    df = pd.DataFrame(
        w.columns, columns=["name", "type", "description", "required", "key", "options", "samples", "source"]
    )
    edited = st.data_editor(
        df,
        key=f"wizard-columns::{w.columns_version}",
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn(
                "Column name", required=True, help="Letters, digits and underscores"
            ),
            "type": st.column_config.SelectboxColumn(
                "Type",
                options=TYPE_OPTIONS,
                required=True,
                help=", ".join(f"{k} = {v}" for k, v in TYPE_LABEL.items()),
            ),
            "description": st.column_config.TextColumn("Description", width="large"),
            "required": st.column_config.CheckboxColumn("Required", default=False),
            "key": st.column_config.CheckboxColumn("Business key", default=False),
            "options": st.column_config.TextColumn(
                "Allowed values", help="Comma-separated; text columns only"
            ),
            "samples": st.column_config.TextColumn("Sample values", disabled=True),
            "source": None,
        },
    )
    if w.parsed is not None and w.parsed.suggested_options:
        st.caption(
            "Allowed values were suggested for low-cardinality text columns; clear the cell to keep free text."
        )
    c1, c2, _ = st.columns([1, 1, 4])
    if c1.button("Back", icon=":material/arrow_back:", width="stretch"):
        w.columns = edited.to_dict("records")
        w.step = 0
        st.rerun()
    if c2.button("Next: details", type="primary", icon=":material/arrow_forward:", width="stretch"):
        rows = edited.to_dict("records")
        columns, errors = _build_columns(rows)
        if errors:
            for e in errors:
                st.error(e)
            w.columns = rows
            return
        w.columns = rows
        w.step = 2
        st.rerun()


def _build_columns(rows: list[dict[str, Any]]) -> tuple[list[ColumnDef], list[str]]:
    columns: list[ColumnDef] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, r in enumerate(rows):
        raw_name = (r.get("name") or "").strip()
        if not raw_name:
            errors.append(f"Row {i + 1}: a column name is required.")
            continue
        name = sanitize_identifier(raw_name)
        if name in seen:
            errors.append(f"Duplicate column name '{name}'.")
            continue
        seen.add(name)
        try:
            dtype = DataType(r.get("type") or "STRING")
        except ValueError:
            errors.append(f"'{name}': unknown type {r.get('type')!r}.")
            continue
        raw_opts = (r.get("options") or "").strip()
        opts = [o.strip() for o in raw_opts.split(",") if o.strip()] if raw_opts else []
        if opts and dtype is not DataType.STRING:
            errors.append(f"'{name}': allowed values are only supported for text columns.")
            continue
        col = ColumnDef(
            name=name,
            data_type=dtype,
            description=(r.get("description") or "").strip(),
            nullable=not bool(r.get("required")),
            options=list(dict.fromkeys(opts)),
            is_key=bool(r.get("key")),
            position=i,
        )
        try:
            col.validate()
        except ValueError as exc:
            errors.append(str(exc))
            continue
        columns.append(col)
    if not columns and not errors:
        errors.append("Define at least one column.")
    return columns, errors


# -- step 3 ---------------------------------------------------------------------------------


def _step_details(ctx: state.AppContext, w: Wizard) -> None:
    st.subheader("Describe the form")
    domains = ctx.permissions.admin_domains
    titles = {}
    for item in state.navigation(ctx, None):
        titles[item.domain.name] = item.domain.title
    c1, c2 = st.columns(2)
    with c1:
        w.domain = st.selectbox(
            "Domain",
            domains,
            index=domains.index(w.domain) if w.domain in domains else 0,
            format_func=lambda d: f"{titles.get(d, humanize(d))} ({d})",
            help="You can only create forms in domains you administer",
        )
        raw_name = st.text_input(
            "Table name", value=w.name, help="lower_snake_case; becomes the Unity Catalog table name"
        )
        w.name = sanitize_identifier(raw_name, fallback="new_form")
        if w.name != raw_name:
            st.caption(f"Will be created as `{w.name}`")
        w.display_name = st.text_input("Display name", value=w.display_name or humanize(w.name))
    with c2:
        w.description = st.text_area(
            "Description", value=w.description, help="Stored as the table comment; searchable in the sidebar"
        )
        w.owner = st.text_input("Owner", value=w.owner or ctx.user.email or ctx.user.username)
        w.effective_dating = st.checkbox(
            "Add effective-dating columns (valid_from, valid_to)",
            value=w.effective_dating,
            help="For lists whose changes must be scheduled ahead of time (slowly changing dimension style).",
        )
        if w.parsed is not None:
            w.load_rows = st.checkbox(
                f"Load the {w.parsed.row_count:,} rows from the file", value=w.load_rows
            )
    b1, b2, _ = st.columns([1, 1, 4])
    if b1.button("Back", icon=":material/arrow_back:", width="stretch"):
        w.step = 1
        st.rerun()
    if b2.button("Next: review", type="primary", icon=":material/arrow_forward:", width="stretch"):
        if not w.name:
            st.error("A table name is required.")
            return
        w.step = 3
        st.rerun()


# -- step 4 ---------------------------------------------------------------------------------


def _assemble(w: Wizard) -> tuple[FormDef, list[ColumnDef], list[str]]:
    columns, errors = _build_columns(w.columns)
    names = {c.name for c in columns}
    if w.effective_dating:
        for extra in EFFECTIVE_DATING_COLUMNS:
            if extra.name not in names:
                columns.append(
                    ColumnDef(extra.name, extra.data_type, extra.description, nullable=extra.nullable)
                )
    form = FormDef(
        domain=w.domain,
        name=w.name,
        display_name=w.display_name.strip(),
        description=w.description.strip(),
        owner=w.owner.strip(),
        columns=columns,
    )
    return form, columns, errors


def _step_review(ctx: state.AppContext, w: Wizard) -> None:
    st.subheader("Review and create")
    form, columns, errors = _assemble(w)
    if errors:
        for e in errors:
            st.error(e)
    c1, c2 = st.columns([1, 1])
    with c1:
        st.markdown(f"**{form.title}**  \n`{form.domain}.{form.name}`")
        st.caption(form.description or "No description")
        st.caption(f"Owner: {form.owner or '-'}")
    with c2:
        st.markdown(f"**{len(columns)} columns** (+ system columns `_id`, `_created_*`, `_updated_*`)")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "column": c.name,
                    "type": c.type_label,
                    "required": c.required,
                    "key": c.is_key,
                    "allowed values": ", ".join(c.options),
                    "description": c.description,
                }
                for c in columns
            ]
        ),
        hide_index=True,
        width="stretch",
    )
    frame = None
    issues: list = []
    if w.parsed is not None and w.load_rows:
        # Each editable row remembers the original header ("source"), so renamed columns still map.
        mapping: dict[str, str] = {}
        by_name = {
            sanitize_identifier((r.get("name") or "").strip()): r
            for r in w.columns
            if (r.get("name") or "").strip()
        }
        for col in columns:
            src = (by_name.get(col.name) or {}).get("source") or ""
            if src and src in w.parsed.raw.columns:
                mapping[col.name] = src
            elif col.name in w.parsed.raw.columns:
                mapping[col.name] = col.name
        frame, issues = coerce_frame(w.parsed.raw, columns, mapping)
        st.markdown(f"**Data:** {len(frame):,} rows will be loaded.")
        if issues:
            st.warning(
                f"{len(issues)} cell(s) do not match the chosen types and will be left empty (or fix the types in step 2):"
            )
            st.markdown("\n".join(f"- {i}" for i in issues[:15]))
        st.dataframe(frame.head(10), hide_index=True, width="stretch")
    else:
        st.markdown("**Data:** the form starts empty.")

    b1, b2, b3, _ = st.columns([1, 1, 1, 3])
    if b1.button("Back", icon=":material/arrow_back:", width="stretch"):
        w.step = 2
        st.rerun()
    if b2.button("Cancel", icon=":material/close:", width="stretch"):
        _reset()
        state.go_home()
        st.rerun()
    if b3.button(
        "Create form", type="primary", icon=":material/add_circle:", width="stretch", disabled=bool(errors)
    ):
        try:
            created = ctx.forms.create_form(form, frame if frame is not None and not frame.empty else None)
        except (BackendError, ValueError) as exc:
            show_error(exc)
            return
        _reset()
        state.invalidate_metadata()
        st.toast(
            f"Form '{created.title}' created with {created.row_count or 0:,} rows",
            icon=":material/check_circle:",
        )
        state.open_form(created.domain, created.name)
        st.rerun()
