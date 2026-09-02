"""Function overview and administration (details, domain, access grants, deletion), plus 'new function'.

A function is one Unity Catalog schema holding forms (tables) and files (CSV/Parquet in its
volume). Function admins edit its details, add files and grant roles; only global admins
create functions, assign them to a domain and delete them.
"""

from __future__ import annotations

import dash_ag_grid as dag
import dash_mantine_components as dmc
from dash import ALL, Input, Output, State, ctx, dcc, html, no_update

from rdm.backend.base import BackendError, NotFoundError, PermissionDenied
from rdm.models import FileDef, FormDef, FunctionDef, Role, humanize, sanitize_identifier, split_file_name
from rdm.services.files import FileError, check_upload, human_size, preview_bytes
from rdm.ui import grid as g
from rdm.ui import ids, uploads
from rdm.ui.components import (
    ROLE_COLORS,
    domain_badge,
    empty_state,
    error_alert,
    icon,
    info_alert,
    link_button,
    notify,
    page_title,
    role_badge,
)
from rdm.ui.context import AppContext, get_context, invalidate_metadata
from rdm.ui.layout import file_href, form_href, function_href

GRID_THEME = "ag-theme-quartz"

GRANTABLE = [Role.VIEWER, Role.EDITOR, Role.ADMIN]


def _domain_options(ctx_: AppContext) -> list[dict[str, str]]:
    return [{"value": d.name, "label": f"{d.title} ({d.name})"} for d in ctx_.forms.list_domains()]


def _domain_title(ctx_: AppContext, name: str) -> str:
    for d in ctx_.forms.list_domains():
        if d.name == name:
            return d.title
    return name


def render(ctx_: AppContext, function_name: str) -> dmc.Stack:
    role = ctx_.role_of(function_name)
    if not role.can_view:
        return dmc.Stack([error_alert("You do not have access to this function.", "Access denied")])
    try:
        function = ctx_.backend.get_function(function_name)
    except NotFoundError as exc:
        return dmc.Stack([error_alert(str(exc), "Not found")])
    forms = ctx_.backend.list_forms(function.name)
    files = ctx_.backend.list_files(function.name)
    right = [role_badge(role)]
    if ctx_.permissions.is_global_admin:
        right.insert(0, dmc.Badge("Global admin", color="orange", variant="light", size="sm"))
    domain_title = _domain_title(ctx_, function.domain) if function.domain else "No domain assigned"
    header = page_title(
        function.title,
        function.description or None,
        crumbs=[dmc.Anchor("Home", href="/"), dmc.Text(domain_title), dmc.Text("Function")],
        right=dmc.Stack(
            [
                dmc.Group(right, gap="xs", justify="flex-end"),
                dmc.Group([domain_badge(domain_title)], justify="flex-end"),
                dmc.Text(
                    f"owner {function.owner}" if function.owner else "", size="xs", c="dimmed", ta="right"
                ),
                dmc.Anchor(
                    dmc.Group(
                        [icon("tabler:external-link", 14), dmc.Text("Project documentation", size="sm")],
                        gap=4,
                    ),
                    href=function.doc_link,
                    target="_blank",
                )
                if function.doc_link
                else None,
            ],
            gap=4,
            align="flex-end",
        ),
    )
    blocks = [
        dcc.Store(id=ids.FUNCTION_KEY, data=function.name),
        header,
        dmc.Group(
            [
                dmc.Title(f"Forms ({len(forms)})", order=4),
                dmc.TextInput(
                    id=ids.FUNCTION_FORMS_FILTER,
                    placeholder="Filter forms",
                    leftSection=icon("tabler:filter"),
                    debounce=250,
                    w=280,
                    size="sm",
                ),
            ],
            justify="space-between",
        ),
        html.Div(id=ids.FUNCTION_FORMS, children=form_cards(function.name, forms, None, role)),
        dmc.Group(
            [
                dmc.Title(f"Files ({len(files)})", order=4),
                dmc.Button(
                    "Add file",
                    id=ids.ADD_FILE_OPEN,
                    variant="light",
                    size="sm",
                    leftSection=icon("tabler:file-plus"),
                    style={} if role.can_admin else {"display": "none"},
                ),
            ],
            justify="space-between",
            mt="sm",
        ),
        dmc.Text(
            "CSV or Parquet datasets too large for a grid: previewed, downloaded and replaced as a whole, "
            "with the same access rules and history as forms.",
            size="xs",
            c="dimmed",
        ),
        html.Div(id=ids.FUNCTION_FILES, children=file_cards(function.name, files, None, role)),
        _add_file_modal(ctx_, function),
    ]
    if role.can_admin:
        blocks += [dmc.Divider(my="md"), _admin(ctx_, function)]
    if ctx_.permissions.can_delete:
        blocks.append(_danger_zone(function, len(forms) + len(files)))
    return dmc.Stack(blocks, gap="md")


def file_cards(
    function: str, files: list[FileDef], text: str | None, role: Role
) -> dmc.SimpleGrid | dmc.Paper:
    needle = (text or "").strip().lower()
    shown = [
        f for f in files if not needle or needle in f"{f.name} {f.title} {f.description} {f.owner}".lower()
    ]
    if not shown:
        if not files:
            return empty_state(
                "No files in this function",
                "Function admins can add a CSV or Parquet file with Add file."
                if role.can_admin
                else "Nothing here yet.",
                "tabler:file-spreadsheet",
            )
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    cards = []
    for f in shown:
        meta = " · ".join(
            x
            for x in [
                f.format.upper(),
                human_size(f.size_bytes),
                f"{f.row_count:,} rows" if f.row_count is not None else "",
                f"owner {f.owner}" if f.owner else "",
            ]
            if x
        )
        cards.append(
            dmc.Card(
                dmc.Stack(
                    [
                        dmc.Group(
                            [
                                icon(
                                    "tabler:file-spreadsheet"
                                    if f.format == "csv"
                                    else "tabler:file-database",
                                    18,
                                ),
                                dmc.Text(f.title, fw=600),
                                dmc.Badge("not registered", size="xs", color="yellow", variant="light")
                                if not f.registered
                                else None,
                            ],
                            gap=6,
                        ),
                        dmc.Text(f.description or "No description", size="sm", c="dimmed", lineClamp=2),
                        dmc.Text(meta, size="xs", c="dimmed"),
                        link_button(
                            "Open",
                            file_href(function, f.name),
                            size="xs",
                            variant="light",
                            leftSection=icon("tabler:external-link", 14),
                        ),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                padding="md",
            )
        )
    return dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3})


def _add_file_modal(ctx_: AppContext, function: FunctionDef) -> dmc.Modal:
    max_mb = ctx_.settings.max_file_mb
    return dmc.Modal(
        id=ids.ADD_FILE_MODAL,
        title=f"Add a file to {function.title}",
        size="xl",
        children=dmc.Stack(
            [
                dcc.Store(id=ids.ADD_FILE_TOKEN, data=None),
                dmc.Text(
                    f"Upload a CSV or Parquet file (up to {max_mb} MB through the browser; larger files can be "
                    "landed in the function's volume directly and appear here automatically). The file is stored "
                    "as a whole; rows are not edited in a grid.",
                    size="sm",
                    c="dimmed",
                ),
                dcc.Upload(
                    id=ids.ADD_FILE_UPLOAD,
                    children=html.Div(
                        ["Drag and drop or ", html.B("click to choose"), " a CSV or Parquet file"]
                    ),
                    className="rdm-dropzone",
                    multiple=False,
                    accept=".csv,.parquet",
                    max_size=max_mb * 1024 * 1024,
                ),
                html.Div(id=ids.ADD_FILE_PREVIEW),
                dmc.SimpleGrid(
                    [
                        dmc.TextInput(
                            id=ids.ADD_FILE_NAME,
                            label="File name",
                            description="lower_snake_case plus .csv or .parquet; the name in the volume",
                            required=True,
                        ),
                        dmc.TextInput(id=ids.ADD_FILE_DISPLAY, label="Display name"),
                        dmc.Textarea(id=ids.ADD_FILE_DESC, label="Description", autosize=True, minRows=2),
                        dmc.TextInput(
                            id=ids.ADD_FILE_OWNER, label="Owner", value=ctx_.user.email or ctx_.user.username
                        ),
                    ],
                    cols={"base": 1, "md": 2},
                ),
                dmc.Group(
                    [
                        dmc.Button(
                            "Add file",
                            id=ids.ADD_FILE_SUBMIT,
                            disabled=True,
                            leftSection=icon("tabler:file-plus"),
                        )
                    ],
                    justify="flex-end",
                ),
            ],
            gap="sm",
        ),
    )


def upload_preview(name: str, data: bytes) -> dmc.Stack:
    """Size, format and the first rows of an uploaded file (parsed locally, before storing)."""
    fmt = split_file_name(name)[1]
    frame = preview_bytes(data, fmt)
    return dmc.Stack(
        [
            dmc.Text(
                f"{name} · {fmt.upper()} · {human_size(len(data))} · {len(frame.columns)} columns "
                f"(showing the first {len(frame)} rows)",
                size="sm",
                fw=500,
            ),
            dag.AgGrid(
                rowData=g.records_from_frame(frame),
                columnDefs=g.frame_column_defs(frame),
                defaultColDef={"resizable": True},
                columnSize="autoSize",
                className=GRID_THEME,
                style={"height": "240px"},
            ),
        ],
        gap="xs",
    )


def form_cards(
    function: str, forms: list[FormDef], text: str | None, role: Role
) -> dmc.SimpleGrid | dmc.Paper:
    needle = (text or "").strip().lower()
    shown = [
        f for f in forms if not needle or needle in f"{f.name} {f.title} {f.description} {f.owner}".lower()
    ]
    if not shown:
        if not forms:
            return empty_state(
                "No forms in this function",
                "Function admins can create one from an Excel file with New form."
                if role.can_admin
                else "Nothing here yet.",
            )
        return empty_state("No matches", "Try another word.", "tabler:search-off")
    cards = []
    for f in shown:
        meta = " · ".join(
            x
            for x in [
                f"~{f.row_count:,} rows" if f.row_count is not None else "",
                f"owner {f.owner}" if f.owner else "",
            ]
            if x
        )
        cards.append(
            dmc.Card(
                dmc.Stack(
                    [
                        dmc.Group([icon("tabler:table", 18), dmc.Text(f.title, fw=600)], gap=6),
                        dmc.Text(f.description or "No description", size="sm", c="dimmed", lineClamp=2),
                        dmc.Text(meta, size="xs", c="dimmed"),
                        link_button(
                            "Open",
                            form_href(function, f.name),
                            size="xs",
                            variant="light",
                            leftSection=icon("tabler:external-link", 14),
                        ),
                    ],
                    gap="xs",
                ),
                withBorder=True,
                radius="md",
                padding="md",
            )
        )
    return dmc.SimpleGrid(cards, cols={"base": 1, "md": 2, "xl": 3})


def _admin(ctx_: AppContext, function: FunctionDef) -> dmc.Stack:
    settings = ctx_.settings
    global_admin = ctx_.permissions.is_global_admin
    form_block = dmc.Paper(
        dmc.Stack(
            [
                dmc.Title("Function settings", order=4),
                dmc.Select(
                    id=ids.FUNCTION_DOMAIN,
                    label="Domain",
                    data=_domain_options(ctx_),
                    value=function.domain or None,
                    placeholder="Not assigned",
                    searchable=True,
                    clearable=True,
                    disabled=not global_admin,
                    description="The business domain this function belongs to (global admins assign it)"
                    if global_admin
                    else "Only global admins can move a function to another domain",
                    leftSection=icon("tabler:sitemap"),
                ),
                dmc.TextInput(
                    id=ids.FUNCTION_DISPLAY,
                    label="Display name",
                    value=function.display_name or function.title,
                ),
                dmc.Textarea(
                    id=ids.FUNCTION_DESC,
                    label="Description",
                    value=function.description,
                    description="Stored as the schema comment",
                    autosize=True,
                    minRows=2,
                ),
                dmc.TextInput(
                    id=ids.FUNCTION_OWNER,
                    label="Owner",
                    value=function.owner,
                    description="Team or person accountable for the lists in this function",
                ),
                dmc.TextInput(
                    id=ids.FUNCTION_DOC_LINK,
                    label="Project documentation link",
                    value=function.doc_link,
                    placeholder="https://...",
                    description="Shown to users on the function page and recorded in the _catalog.functions registry",
                    leftSection=icon("tabler:link"),
                ),
                dmc.Group(
                    [dmc.Button("Save", id=ids.FUNCTION_SAVE, leftSection=icon("tabler:device-floppy"))]
                ),
                html.Div(id=ids.FUNCTION_RESULT),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )
    grants_note = (
        "Roles are Unity Catalog privileges on this schema, granted to Databricks groups. Granting a role here runs GRANT/REVOKE on the schema; the table reflects information_schema."
        if settings.is_databricks
        else "Roles are granted to groups (locally emulated in the _catalog.grants table)."
    )
    groups = ctx_.forms.list_groups()
    if groups:
        picker = dmc.Select(
            id=ids.GRANT_PRINCIPAL,
            data=[{"value": g, "label": g} for g in groups],
            placeholder="Search a group",
            searchable=True,
            clearable=True,
            nothingFoundMessage="No group with that name",
            w=320,
            leftSection=icon("tabler:users-group"),
        )
    else:
        picker = dmc.TextInput(
            id=ids.GRANT_PRINCIPAL,
            placeholder="Exact Databricks group name",
            description="Group lookup is unavailable here; type the exact group name.",
            w=320,
            leftSection=icon("tabler:users-group"),
        )
    grants_block = dmc.Paper(
        dmc.Stack(
            [
                dmc.Group(
                    [
                        dmc.Title("Access", order=4),
                        dmc.TextInput(
                            id=ids.GRANTS_FILTER,
                            placeholder="Filter groups",
                            leftSection=icon("tabler:filter"),
                            debounce=250,
                            w=240,
                            size="sm",
                        ),
                    ],
                    justify="space-between",
                ),
                dmc.Text(
                    grants_note + " Individual accounts cannot be granted access.", size="sm", c="dimmed"
                ),
                html.Div(id=ids.GRANTS_TABLE, children=grants_table(ctx_, function.name, None)),
                dmc.Group(
                    [
                        picker,
                        dmc.Select(
                            id=ids.GRANT_ROLE,
                            data=[{"value": r.name, "label": r.label} for r in GRANTABLE],
                            value=Role.VIEWER.name,
                            w=180,
                            allowDeselect=False,
                            searchable=True,
                        ),
                        dmc.Button(
                            "Grant",
                            id=ids.GRANT_SUBMIT,
                            leftSection=icon("tabler:user-plus"),
                            variant="light",
                        ),
                    ],
                    align="flex-end",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
    )
    return dmc.Stack([form_block, grants_block], gap="md")


def _danger_zone(function: FunctionDef, n_forms: int) -> dmc.Paper:
    """Deleting a function (dropping its schema) is a global-admin action and needs an empty function."""
    return dmc.Paper(
        dmc.Stack(
            [
                dmc.Title("Danger zone", order=4, c="red"),
                dmc.Text(
                    f"Delete the function '{function.title}' (schema {function.name}). "
                    + (
                        f"It still holds {n_forms} form(s): delete or migrate them first."
                        if n_forms
                        else "Its grants and registry entry are removed as well. This cannot be undone."
                    ),
                    size="sm",
                ),
                dmc.Group(
                    [
                        dmc.TextInput(
                            id=ids.DROP_FUNCTION_CONFIRM,
                            placeholder=f"type {function.name} to confirm",
                            w=360,
                        ),
                        dmc.Button(
                            "Delete function",
                            id=ids.DROP_FUNCTION_SUBMIT,
                            color="red",
                            disabled=bool(n_forms),
                            leftSection=icon("tabler:trash-x"),
                        ),
                    ],
                    align="flex-end",
                ),
            ],
            gap="sm",
        ),
        withBorder=True,
        p="md",
        radius="md",
        style={"borderColor": "#fa5252"},
    )


def grants_table(ctx_: AppContext, function: str, text: str | None) -> dmc.Table | dmc.Text | dmc.Alert:
    try:
        grants = ctx_.forms.list_function_grants(function)
    except BackendError as exc:
        return error_alert(exc)
    needle = (text or "").strip().lower()
    grants = [(p, r) for p, r in grants if not needle or needle in p.lower()]
    if not grants:
        return dmc.Text(
            "No explicit grants on this function." if not needle else "No matching groups.",
            size="sm",
            c="dimmed",
        )
    rows = []
    for principal, role in grants:
        rows.append(
            dmc.TableTr(
                [
                    dmc.TableTd(
                        dmc.Group([icon("tabler:users-group", 16), dmc.Text(principal, size="sm")], gap=6)
                    ),
                    dmc.TableTd(dmc.Badge(role.label, color=ROLE_COLORS[role], variant="light", size="sm")),
                    dmc.TableTd(
                        dmc.Button(
                            "Revoke",
                            id=ids.revoke_id(principal),
                            size="xs",
                            variant="subtle",
                            color="red",
                            leftSection=icon("tabler:user-minus", 14),
                        )
                    ),
                ]
            )
        )
    return dmc.Table(
        [
            dmc.TableThead(dmc.TableTr([dmc.TableTh("Group"), dmc.TableTh("Role"), dmc.TableTh("")])),
            dmc.TableTbody(rows),
        ],
        striped=True,
        highlightOnHover=True,
        withTableBorder=True,
    )


def render_new(ctx_: AppContext) -> dmc.Stack:
    header = page_title(
        "New function",
        "A function is a Unity Catalog schema that groups the forms of one business function. "
        "Functions belong to a domain.",
    )
    if not ctx_.permissions.can_create_function:
        return dmc.Stack(
            [
                header,
                info_alert(
                    "Creating functions requires global administrator rights (CREATE SCHEMA on the catalog).",
                    "Global admins only",
                    "yellow",
                ),
            ]
        )
    domains = _domain_options(ctx_)
    return dmc.Stack(
        [
            header,
            dmc.Paper(
                dmc.Stack(
                    [
                        dmc.Select(
                            id=ids.NEW_FUNCTION_DOMAIN,
                            label="Domain",
                            data=domains,
                            value=domains[0]["value"] if domains else None,
                            placeholder="Choose a domain" if domains else "No domains yet - create one first",
                            description="Every function belongs to a business domain (global admins maintain the list under Domains)",
                            searchable=True,
                            clearable=True,
                            leftSection=icon("tabler:sitemap"),
                        ),
                        dmc.TextInput(
                            id=ids.NEW_FUNCTION_NAME,
                            label="Name",
                            placeholder="e.g. student__survey_service_improvement",
                            description="Convention: <domain>__<area>; becomes the schema name and cannot change",
                            required=True,
                        ),
                        dmc.TextInput(
                            id=ids.NEW_FUNCTION_DISPLAY,
                            label="Display name",
                            placeholder="Student Survey & Service Improvement",
                        ),
                        dmc.Textarea(id=ids.NEW_FUNCTION_DESC, label="Description", autosize=True, minRows=2),
                        dmc.TextInput(
                            id=ids.NEW_FUNCTION_OWNER,
                            label="Owner",
                            value=ctx_.user.email or ctx_.user.username,
                            description="Team or person accountable for the function",
                        ),
                        dmc.TextInput(
                            id=ids.NEW_FUNCTION_DOC_LINK,
                            label="Project documentation link",
                            placeholder="https://...",
                            leftSection=icon("tabler:link"),
                        ),
                        dmc.Group(
                            [
                                dmc.Button(
                                    "Create function",
                                    id=ids.NEW_FUNCTION_SUBMIT,
                                    leftSection=icon("tabler:folder-plus"),
                                )
                            ]
                        ),
                        html.Div(id=ids.NEW_FUNCTION_RESULT),
                    ],
                    gap="sm",
                ),
                withBorder=True,
                p="md",
                radius="md",
                maw=720,
            ),
        ]
    )


def register(app) -> None:
    @app.callback(
        Output(ids.FUNCTION_FORMS, "children"),
        Output(ids.FUNCTION_FILES, "children"),
        Input(ids.FUNCTION_FORMS_FILTER, "value"),
        State(ids.FUNCTION_KEY, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def filter_forms(text, function, persona):
        c = get_context(persona)
        role = c.role_of(function)
        return (
            form_cards(function, c.backend.list_forms(function), text, role),
            file_cards(function, c.backend.list_files(function), text, role),
        )

    @app.callback(
        Output(ids.ADD_FILE_MODAL, "opened"), Input(ids.ADD_FILE_OPEN, "n_clicks"), prevent_initial_call=True
    )
    def open_add_file(n):
        return bool(n)

    @app.callback(
        Output(ids.ADD_FILE_PREVIEW, "children"),
        Output(ids.ADD_FILE_TOKEN, "data"),
        Output(ids.ADD_FILE_NAME, "value"),
        Output(ids.ADD_FILE_DISPLAY, "value"),
        Output(ids.ADD_FILE_SUBMIT, "disabled"),
        Input(ids.ADD_FILE_UPLOAD, "contents"),
        State(ids.ADD_FILE_UPLOAD, "filename"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def preview_new_file(contents, filename, persona):
        if not contents:
            return no_update, no_update, no_update, no_update, True
        c = get_context(persona)
        token = uploads.put_data_url(filename or "upload", contents)
        _name, data = uploads.get(token)
        try:
            name = check_upload(filename or "", data, c.settings.max_file_mb)
            preview = upload_preview(name, data)
        except FileError as exc:
            uploads.drop(token)
            return error_alert(exc, "Cannot use this file"), None, "", "", True
        return preview, token, name, humanize(split_file_name(name)[0]), False

    @app.callback(
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.ADD_FILE_PREVIEW, "children", allow_duplicate=True),
        Input(ids.ADD_FILE_SUBMIT, "n_clicks"),
        State(ids.ADD_FILE_TOKEN, "data"),
        State(ids.ADD_FILE_NAME, "value"),
        State(ids.ADD_FILE_DISPLAY, "value"),
        State(ids.ADD_FILE_DESC, "value"),
        State(ids.ADD_FILE_OWNER, "value"),
        State(ids.FUNCTION_KEY, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def add_file(n, token, raw_name, display, desc, owner, function, nav_version, persona):
        if not n:
            return no_update, no_update, no_update, no_update
        stored = uploads.get(token)
        if stored is None:
            return no_update, notify("Upload a file first", color="yellow"), no_update, no_update
        _filename, data = stored
        c = get_context(persona)
        try:
            name = check_upload(raw_name or "", data, c.settings.max_file_mb)
            created = c.forms.add_file(
                FileDef(
                    function,
                    name,
                    display_name=(display or "").strip(),
                    description=(desc or "").strip(),
                    owner=(owner or "").strip(),
                ),
                data,
            )
        except (BackendError, PermissionDenied, FileError, ValueError) as exc:
            return no_update, no_update, no_update, error_alert(exc, "Cannot add the file")
        uploads.drop(token)
        invalidate_metadata()
        return (
            file_href(created.function, created.name),
            notify(f"File '{created.title}' added ({created.row_count or 0:,} rows)"),
            (nav_version or 0) + 1,
            None,
        )

    @app.callback(
        Output(ids.FUNCTION_RESULT, "children"),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.FUNCTION_SAVE, "n_clicks"),
        State(ids.FUNCTION_KEY, "data"),
        State(ids.FUNCTION_DOMAIN, "value"),
        State(ids.FUNCTION_DISPLAY, "value"),
        State(ids.FUNCTION_DESC, "value"),
        State(ids.FUNCTION_OWNER, "value"),
        State(ids.FUNCTION_DOC_LINK, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def save_function(n, name, domain, display, desc, owner, doc_link, nav_version, persona):
        if not n:
            return no_update, no_update, no_update
        c = get_context(persona)
        try:
            function = c.backend.get_function(name)
            function.display_name, function.description, function.owner = (
                (display or "").strip(),
                (desc or "").strip(),
                (owner or "").strip(),
            )
            function.doc_link = (doc_link or "").strip()
            if c.permissions.is_global_admin:
                function.domain = (domain or "").strip()
            c.forms.update_function(function)
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc), no_update, no_update
        invalidate_metadata()
        return None, (nav_version or 0) + 1, notify("Function saved")

    @app.callback(
        Output(ids.GRANTS_TABLE, "children"),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.GRANT_SUBMIT, "n_clicks"),
        Input({"type": "revoke", "principal": ALL}, "n_clicks"),
        Input(ids.GRANTS_FILTER, "value"),
        State(ids.FUNCTION_KEY, "data"),
        State(ids.GRANT_PRINCIPAL, "value"),
        State(ids.GRANT_ROLE, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def change_grants(n_grant, n_revokes, filter_text, function, principal, role_name, nav_version, persona):
        trigger = ctx.triggered_id
        c = get_context(persona)
        if trigger == ids.GRANTS_FILTER:
            return grants_table(c, function, filter_text), no_update, no_update
        if trigger is None or (not any(n_revokes or []) and not n_grant):
            return no_update, no_update, no_update
        try:
            if trigger == ids.GRANT_SUBMIT:
                if not (principal or "").strip():
                    return no_update, notify("Choose a group first", color="red"), no_update
                c.forms.grant_function_role(function, principal.strip(), Role[role_name])
                message = f"Granted {Role[role_name].label} to {principal.strip()}"
            else:
                c.forms.grant_function_role(function, trigger["principal"], Role.NONE)
                message = f"Revoked access of {trigger['principal']}"
        except (BackendError, PermissionDenied, ValueError, KeyError) as exc:
            return no_update, notify(str(exc), title="Not applied", color="red"), no_update
        invalidate_metadata()
        return (
            grants_table(get_context(persona), function, filter_text),
            notify(message),
            (nav_version or 0) + 1,
        )

    @app.callback(
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Input(ids.DROP_FUNCTION_SUBMIT, "n_clicks"),
        State(ids.DROP_FUNCTION_CONFIRM, "value"),
        State(ids.FUNCTION_KEY, "data"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def drop_function(n, confirm, name, nav_version, persona):
        if not n:
            return no_update, no_update, no_update
        if (confirm or "").strip() != name:
            return no_update, notify("Type the function name exactly to confirm", color="yellow"), no_update
        c = get_context(persona)
        try:
            function = c.backend.get_function(name)
            c.forms.drop_function(function)
        except (BackendError, PermissionDenied) as exc:
            return no_update, notify(str(exc), title="Not deleted", color="red"), no_update
        invalidate_metadata()
        return "/", notify(f"Function '{function.title}' deleted", color="gray"), (nav_version or 0) + 1

    @app.callback(
        Output(ids.NEW_FUNCTION_RESULT, "children"),
        Output(ids.URL, "pathname", allow_duplicate=True),
        Output(ids.NAV_VERSION, "data", allow_duplicate=True),
        Output(ids.NOTIFY, "sendNotifications", allow_duplicate=True),
        Input(ids.NEW_FUNCTION_SUBMIT, "n_clicks"),
        State(ids.NEW_FUNCTION_DOMAIN, "value"),
        State(ids.NEW_FUNCTION_NAME, "value"),
        State(ids.NEW_FUNCTION_DISPLAY, "value"),
        State(ids.NEW_FUNCTION_DESC, "value"),
        State(ids.NEW_FUNCTION_OWNER, "value"),
        State(ids.NEW_FUNCTION_DOC_LINK, "value"),
        State(ids.NAV_VERSION, "data"),
        State(ids.PERSONA, "data"),
        prevent_initial_call=True,
    )
    def create_function(n, domain, raw_name, display, desc, owner, doc_link, nav_version, persona):
        if not n:
            return no_update, no_update, no_update, no_update
        name = sanitize_identifier(raw_name or "", fallback="")
        if not name:
            return error_alert("A name is required.", "Cannot create"), no_update, no_update, no_update
        c = get_context(persona)
        try:
            function = c.forms.create_function(
                FunctionDef(
                    name,
                    (display or "").strip(),
                    (desc or "").strip(),
                    (owner or "").strip(),
                    doc_link=(doc_link or "").strip(),
                    domain=(domain or "").strip(),
                )
            )
        except (BackendError, PermissionDenied, ValueError) as exc:
            return error_alert(exc, "Cannot create"), no_update, no_update, no_update
        invalidate_metadata()
        return (
            None,
            function_href(function.name),
            (nav_version or 0) + 1,
            notify(f"Function '{function.title}' created"),
        )
