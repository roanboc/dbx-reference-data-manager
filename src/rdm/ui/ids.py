"""Component ids. Kept in one place so callbacks and layouts cannot drift apart."""

# shell
SHELL = "shell"  # MantineProvider; forceColorScheme is driven by the scheme toggle
SCHEME_TOGGLE = "scheme-toggle"  # light / dark, seeded from the OS and persisted per browser (FR-37)
URL = "url"
PERSONA = "persona-store"
PERSONA_SELECT = "persona-select"
NAV_VERSION = "nav-version"
NAV_SEARCH = "nav-search"
NAVBAR = "navbar-content"
NAV_RESIZER = "nav-resizer"  # draggable sidebar edge (assets/navbar_resize.js)
PAGE = "page-content"
NOTIFY = "notifications"
DOWNLOAD = "download"

# form page
FORM_KEY = "form-key"
DRAFT = "draft-store"
GRID_VERSION = "grid-version"
GRID = "grid"
GRID_SEARCH = "grid-search"
GRID_AUDIT = "grid-audit"
GRID_REFRESH = "grid-refresh"
GRID_ADD = "grid-add"
GRID_DELETE = "grid-delete"
GRID_SAVE = "grid-save"
GRID_DISCARD = "grid-discard"
GRID_CSV = "grid-export-csv"
GRID_XLSX = "grid-export-xlsx"
GRID_CAPTION = "grid-caption"
PENDING = "pending-bar"
SAVE_RESULT = "save-result"
FORM_TABS = "form-tabs"
HISTORY_PANEL = "history-panel"
SCHEMA_PANEL = "schema-panel"
SCHEMA_GRID = "schema-grid"
SCHEMA_SAVE = "schema-save"
SCHEMA_RESULT = "schema-result"
ADD_COL_OPEN = "add-col-open"
ADD_COL_MODAL = "add-col-modal"
ADD_COL_NAME = "add-col-name"
ADD_COL_TYPE = "add-col-type"
ADD_COL_DESC = "add-col-desc"
ADD_COL_PRECISION = "add-col-precision"
ADD_COL_SCALE = "add-col-scale"
ADD_COL_SUBMIT = "add-col-submit"
DROP_COL_OPEN = "drop-col-open"
DROP_COL_MODAL = "drop-col-modal"
DROP_COL_SELECT = "drop-col-select"
DROP_COL_CONFIRM = "drop-col-confirm"
DROP_COL_SUBMIT = "drop-col-submit"
SETTINGS_DISPLAY = "settings-display-name"
SETTINGS_DESC = "settings-description"
SETTINGS_OWNER = "settings-owner"
SETTINGS_OWNER_EMAIL = "settings-owner-email"
SETTINGS_SAVE = "settings-save"
SCD2_SWITCH = "scd2-switch"  # optional Type 2 history table (FR-47)
SETTINGS_RESULT = "settings-result"
DROP_FORM_CONFIRM = "drop-form-confirm"
DROP_FORM_SUBMIT = "drop-form-submit"
IMPORT_OPEN = "import-open"
IMPORT_MODAL = "import-modal"
IMPORT_MODE = "import-mode"  # append | merge | replace (FR-43)
IMPORT_UPLOAD = "import-upload"
IMPORT_SHEET = "import-sheet"
IMPORT_PREVIEW = "import-preview"
IMPORT_SUBMIT = "import-submit"
IMPORT_TOKEN = "import-token"

# form page: bulk update of selected rows (FR-22)
BULK_OPEN = "bulk-open"
BULK_MODAL = "bulk-modal"
BULK_INFO = "bulk-info"
BULK_COLUMN = "bulk-column"
BULK_VALUE_WRAP = "bulk-value-wrap"
BULK_VALUE = "bulk-value"
BULK_CLEAR = "bulk-clear"
BULK_SUBMIT = "bulk-submit"

# form page: item form for one row, with per-row history and restore (FR-24)
ITEM_OPEN = "item-open"
ITEM_MODAL = "item-modal"
ITEM_BODY = "item-body"
ITEM_ROW = "item-row"  # store: the row the item form was opened on
ITEM_HISTORY = "item-history"  # store: history records of that row (for restore)
ITEM_SAVE = "item-save"
ITEM_RESULT = "item-result"


def item_field_id(column: str) -> dict:
    return {"type": "item-field", "column": column}


def restore_id(version: int) -> dict:
    """Restore button of one history entry in the item form."""
    return {"type": "restore", "version": int(version)}


def history_grid_id(form: str) -> dict:
    return {"type": "history-grid", "form": form}


def history_filter_id(form: str) -> dict:
    return {"type": "history-filter", "form": form}


def history_restore_id(form: str) -> dict:
    """Restore button of the History tab (restores the selected entry, deleted rows included)."""
    return {"type": "history-restore", "form": form}


# form creator
WIZ_STORE = "wizard-store"
WIZ_STEPPER = "wizard-stepper"
WIZ_BODY = "wizard-body"
WIZ_MODE = "wiz-mode"
WIZ_UPLOAD = "wiz-upload"
WIZ_SHEET = "wiz-sheet"
WIZ_HEADER_ROW = "wiz-header-row"
WIZ_PREVIEW = "wiz-preview"
WIZ_NEXT = "wiz-next"
WIZ_BACK = "wiz-back"
WIZ_CANCEL = "wiz-cancel"
WIZ_COLUMNS_GRID = "wiz-columns-grid"
WIZ_ADD_COLUMN = "wiz-add-column"
WIZ_FUNCTION = "wiz-function"
WIZ_NAME = "wiz-name"
WIZ_DISPLAY = "wiz-display"
WIZ_DESC = "wiz-desc"
WIZ_OWNER = "wiz-owner"
WIZ_OWNER_EMAIL = "wiz-owner-email"
WIZ_CREATE = "wiz-create"
WIZ_ERRORS = "wiz-errors"

# home / help
HOME_FILTER = "home-filter"
HOME_CARDS = "home-cards"
HELP_TABS = "help-tabs"

# function pages (a function is a Unity Catalog schema)
FUNCTION_KEY = "function-key"
FUNCTION_DOC_LINK = "function-doc-link"
FUNCTION_FORMS_FILTER = "function-forms-filter"
FUNCTION_FORMS = "function-forms"
FUNCTION_FILES = "function-files"
ADD_FILE_OPEN = "add-file-open"
NEW_FILE_FUNCTION = "new-file-function"
ADD_FILE_MODAL = "add-file-modal"
ADD_FILE_UPLOAD = "add-file-upload"
ADD_FILE_TOKEN = "add-file-token"
ADD_FILE_NAME = "add-file-name"
ADD_FILE_DISPLAY = "add-file-display"
ADD_FILE_DESC = "add-file-desc"
ADD_FILE_OWNER = "add-file-owner"
ADD_FILE_OWNER_EMAIL = "add-file-owner-email"
ADD_FILE_VALIDATE = "add-file-validate"
ADD_FILE_PREVIEW = "add-file-preview"
ADD_FILE_SUBMIT = "add-file-submit"

# file page (a CSV / Parquet file in the function's volume)
FILE_KEY = "file-key"
FILE_TABS = "file-tabs"
FILE_PREVIEW = "file-preview"
FILE_PREVIEW_GRID = "file-preview-grid"
FILE_COLUMNS_PANEL = "file-columns-panel"
FILE_HISTORY_PANEL = "file-history-panel"
FILE_DOWNLOAD = "file-download"
FILE_REPLACE_OPEN = "file-replace-open"
FILE_REPLACE_MODAL = "file-replace-modal"
FILE_REPLACE_UPLOAD = "file-replace-upload"
FILE_REPLACE_TOKEN = "file-replace-token"
FILE_REPLACE_VALIDATE = "file-replace-validate"
FILE_REPLACE_PREVIEW = "file-replace-preview"
FILE_REPLACE_SUBMIT = "file-replace-submit"
FILE_SETTINGS_DISPLAY = "file-settings-display"
FILE_SETTINGS_DESC = "file-settings-desc"
FILE_SETTINGS_OWNER = "file-settings-owner"
FILE_SETTINGS_OWNER_EMAIL = "file-settings-owner-email"
FILE_SETTINGS_SAVE = "file-settings-save"
FILE_SETTINGS_RESULT = "file-settings-result"
DROP_FILE_CONFIRM = "drop-file-confirm"
DROP_FILE_SUBMIT = "drop-file-submit"
FUNCTION_DISPLAY = "function-display-name"
FUNCTION_DESC = "function-description"
FUNCTION_OWNER = "function-owner"
FUNCTION_OWNER_EMAIL = "function-owner-email"
FUNCTION_DOMAIN = "function-domain"
FUNCTION_SAVE = "function-save"
FUNCTION_RESULT = "function-result"
DROP_FUNCTION_CONFIRM = "drop-function-confirm"
DROP_FUNCTION_SUBMIT = "drop-function-submit"
GRANTS_FILTER = "grants-filter"
GRANT_PRINCIPAL = "grant-principal"
GRANT_ROLE = "grant-role"
GRANT_SUBMIT = "grant-submit"
GRANTS_TABLE = "grants-table"
NEW_FUNCTION_NAME = "new-function-name"
NEW_FUNCTION_DISPLAY = "new-function-display"
NEW_FUNCTION_DESC = "new-function-desc"
NEW_FUNCTION_OWNER = "new-function-owner"
NEW_FUNCTION_OWNER_EMAIL = "new-function-owner-email"
NEW_FUNCTION_DOC_LINK = "new-function-doc-link"
NEW_FUNCTION_DOMAIN = "new-function-domain"
NEW_FUNCTION_SUBMIT = "new-function-submit"
NEW_FUNCTION_RESULT = "new-function-result"

# domain overview page
DOMAIN_KEY = "domain-key"
DOMAIN_FUNCTIONS = "domain-functions"
DOMAIN_FUNCTIONS_FILTER = "domain-functions-filter"

# domains page (global admins administer the domain list)
DOMAIN_EDITING = "domain-editing"  # store: name of the domain being edited, or None
DOMAIN_FORM_TITLE = "domain-form-title"
DOMAIN_NAME = "domain-name"
DOMAIN_DISPLAY = "domain-display"
DOMAIN_DESC = "domain-desc"
DOMAIN_OWNER = "domain-owner"
DOMAIN_SUBMIT = "domain-submit"
DOMAIN_CANCEL = "domain-cancel"
DOMAIN_RESULT = "domain-result"
DOMAIN_DELETE_ZONE = "domain-delete-zone"
DOMAIN_DELETE_CONFIRM = "domain-delete-confirm"
DOMAIN_DELETE_SUBMIT = "domain-delete-submit"
DOMAINS_FILTER = "domains-filter"
DOMAINS_TABLE = "domains-table"


def domain_edit_id(name: str) -> dict:
    return {"type": "domain-edit", "name": name}


def revoke_id(principal: str) -> dict:
    return {"type": "revoke", "principal": principal}
