"""Small presentational helpers shared by the views."""

from __future__ import annotations

import streamlit as st

from rdm.backend.base import BackendError
from rdm.models import DataType, FormDef, Role

ROLE_COLORS = {Role.ADMIN: "violet", Role.EDITOR: "green", Role.VIEWER: "blue", Role.NONE: "grey"}
ROLE_ICONS = {
    Role.ADMIN: ":material/admin_panel_settings:",
    Role.EDITOR: ":material/edit:",
    Role.VIEWER: ":material/visibility:",
    Role.NONE: ":material/block:",
}
TYPE_ICONS = {
    DataType.STRING: ":material/text_fields:",
    DataType.INTEGER: ":material/tag:",
    DataType.DECIMAL: ":material/attach_money:",
    DataType.DOUBLE: ":material/functions:",
    DataType.BOOLEAN: ":material/check_box:",
    DataType.DATE: ":material/calendar_today:",
    DataType.TIMESTAMP: ":material/schedule:",
    DataType.OTHER: ":material/data_object:",
}

CSS = """
<style>
/* Tighter sidebar navigation */
section[data-testid="stSidebar"] div[data-testid="stExpander"] details summary p { font-weight: 600; }
section[data-testid="stSidebar"] .stButton button[kind="tertiary"] {
    justify-content: flex-start; text-align: left; padding: 0.15rem 0.4rem; white-space: normal;
}
section[data-testid="stSidebar"] .stButton button[kind="primary"] {
    justify-content: flex-start; text-align: left; padding: 0.15rem 0.4rem; white-space: normal;
}
/* Header spacing */
h1, h2, h3 { margin-bottom: 0.2rem; }
div[data-testid="stCaptionContainer"] { margin-top: -0.2rem; }
/* Pending changes bar */
.rdm-pending { padding: 0.6rem 0.9rem; border-radius: 0.5rem; background: #FFF4E5; border: 1px solid #F5C77E; }
.rdm-muted { color: #6B7280; font-size: 0.85rem; }
</style>
"""


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def role_badge(role: Role) -> None:
    st.badge(role.label, icon=ROLE_ICONS[role], color=ROLE_COLORS[role])


def role_badge_md(role: Role) -> str:
    return f":{ROLE_COLORS[role]}-badge[{role.label}]"


def show_error(exc: Exception, prefix: str = "") -> None:
    message = str(exc) if isinstance(exc, BackendError | ValueError) else f"Unexpected error: {exc}"
    st.error(f"{prefix}{message}", icon=":material/error:")


def form_meta_line(form: FormDef) -> str:
    bits = []
    if form.row_count is not None:
        bits.append(f"{form.row_count:,} rows")
    if form.owner:
        bits.append(f"owner {form.owner}")
    if form.updated_at is not None:
        who = f" by {form.updated_by}" if form.updated_by else ""
        bits.append(f"updated {form.updated_at:%Y-%m-%d %H:%M}{who}")
    return " · ".join(bits)


def empty_state(title: str, body: str, icon: str = ":material/inbox:") -> None:
    with st.container(border=True):
        st.markdown(f"#### {icon} {title}")
        st.caption(body)
