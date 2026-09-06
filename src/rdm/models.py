"""Domain model shared by the UI, the services and every backend.

Nothing in this module knows about SQL or Dash. The hierarchy is *domain* (a business
classifier maintained by global admins) > *function* (a Unity Catalog schema) > *form* (a
Delta table) or *file* (a CSV/Parquet file in the function's volume, for lists too large to
manage in a grid). Backends translate these objects into their own DDL/DML; the UI renders
them.
"""

from __future__ import annotations

import enum
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# --------------------------------------------------------------------------------------
# Naming rules
# --------------------------------------------------------------------------------------

#: Identifiers (domains, functions, forms, columns) are lower_snake_case, max 63 chars. This is the
#: intersection of what Unity Catalog and DuckDB accept without quoting surprises.
IDENTIFIER_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_IDENTIFIER_LENGTH = 63

#: Words that are awkward as column names in either SQL dialect. Sanitising appends "_".
RESERVED_WORDS = frozenset(
    {
        "all",
        "and",
        "any",
        "as",
        "asc",
        "between",
        "by",
        "case",
        "cast",
        "check",
        "constraint",
        "create",
        "cross",
        "current",
        "date",
        "default",
        "delete",
        "desc",
        "distinct",
        "drop",
        "else",
        "end",
        "escape",
        "except",
        "exists",
        "false",
        "fetch",
        "filter",
        "for",
        "foreign",
        "from",
        "full",
        "grant",
        "group",
        "having",
        "in",
        "inner",
        "insert",
        "intersect",
        "interval",
        "into",
        "is",
        "join",
        "lateral",
        "left",
        "like",
        "limit",
        "natural",
        "not",
        "null",
        "of",
        "on",
        "only",
        "or",
        "order",
        "outer",
        "primary",
        "references",
        "right",
        "select",
        "table",
        "then",
        "time",
        "timestamp",
        "to",
        "true",
        "union",
        "unique",
        "update",
        "user",
        "using",
        "values",
        "when",
        "where",
        "with",
    }
)

#: System columns carried by every form created through the app (SharePoint style).
ID_COLUMN = "_id"
VERSION_COLUMN = "_version"
CREATED_AT_COLUMN = "_created_at"
CREATED_BY_COLUMN = "_created_by"
UPDATED_AT_COLUMN = "_updated_at"
UPDATED_BY_COLUMN = "_updated_by"
SYSTEM_COLUMNS: tuple[str, ...] = (
    ID_COLUMN,
    VERSION_COLUMN,
    CREATED_AT_COLUMN,
    CREATED_BY_COLUMN,
    UPDATED_AT_COLUMN,
    UPDATED_BY_COLUMN,
)
AUDIT_COLUMNS: tuple[str, ...] = (CREATED_AT_COLUMN, CREATED_BY_COLUMN, UPDATED_AT_COLUMN, UPDATED_BY_COLUMN)

#: Columns returned by ``DatabaseBackend.get_history`` in front of the row columns.
HISTORY_COLUMNS: tuple[str, ...] = ("version", "changed_at", "changed_by", "change_type")


def is_system_column(name: str) -> bool:
    return name in SYSTEM_COLUMNS


def validate_identifier(name: str, kind: str = "identifier", allow_leading_underscore: bool = True) -> str:
    """Return ``name`` if it is a safe identifier, else raise ``ValueError``.

    Leading underscores are reserved for system objects (``_id``, ``_catalog``, the
    ``_reference_data`` catalog); user-created names must start with a letter.
    """
    if not isinstance(name, str) or not IDENTIFIER_RE.match(name):
        raise ValueError(
            f"Invalid {kind} {name!r}: use lower-case letters, digits and underscores, "
            f"start with a letter, max {MAX_IDENTIFIER_LENGTH} characters."
        )
    if not allow_leading_underscore and name.startswith("_"):
        raise ValueError(f"Invalid {kind} {name!r}: names starting with '_' are reserved for system use.")
    return name


def sanitize_identifier(raw: Any, fallback: str = "column") -> str:
    """Turn free text (an Excel header, a sheet name) into a safe identifier.

    ``"Cost Centre (GBP)"`` -> ``"cost_centre_gbp"``; ``"2024 Budget"`` -> ``"col_2024_budget"``.
    """
    text = "" if raw is None else str(raw)
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9_]+", "_", text)
    # keep deliberate double underscores (function convention <domain>__<area>), fold longer runs
    text = re.sub(r"_{3,}", "__", text).strip("_")
    if not text:
        text = fallback
    if not text[0].isalpha():
        text = f"col_{text}"
    text = text[:MAX_IDENTIFIER_LENGTH].rstrip("_") or fallback
    if text in RESERVED_WORDS:
        text = f"{text[: MAX_IDENTIFIER_LENGTH - 1]}_"
    return text


#: File formats accepted for files (large reference datasets kept as files, not as forms).
FILE_FORMATS: tuple[str, ...] = ("csv", "parquet")
#: Managed volume the app creates in every function schema for its files.
FILES_VOLUME = "_files"


def qualified_name(*parts: str) -> str:
    r"""Back-quoted Databricks name, e.g. ``qualified_name(catalog, function, form)`` -> ``\`c\`.\`f\`.\`t\```.

    Every part is validated first, so the result can be pasted into a query as is.
    """
    return ".".join(f"`{validate_identifier(p)}`" for p in parts)


def volume_file_path(catalog: str, function: str, name: str) -> str:
    """Path of a file in the function's volume: ``/Volumes/<catalog>/<function>/_files/<name>``."""
    validate_identifier(catalog, "catalog name")
    validate_identifier(function, "function name", allow_leading_underscore=False)
    validate_file_name(name)
    return f"/Volumes/{catalog}/{function}/{FILES_VOLUME}/{name}"


def split_file_name(name: str) -> tuple[str, str]:
    """``"gl_transactions.csv"`` -> ``("gl_transactions", "csv")``; no extension -> ``("...", "")``."""
    stem, _, ext = (name or "").rpartition(".")
    if not stem:
        return ext, ""
    return stem, ext.lower()


def sanitize_file_name(raw: Any, fallback: str = "file") -> str:
    """Turn an uploaded file name into ``<identifier>.<format>`` (``"GL Transactions 2024.CSV"`` -> ``"gl_transactions_2024.csv"``)."""
    text = "" if raw is None else str(raw)
    base = text.replace("\\", "/").rsplit("/", 1)[-1]
    stem, ext = split_file_name(base)
    stem = sanitize_identifier(stem, fallback=fallback)
    return f"{stem}.{ext}" if ext else stem


def validate_file_name(name: str) -> str:
    """A file name is ``<identifier>.<format>`` with a supported format."""
    stem, ext = split_file_name(name)
    if ext not in FILE_FORMATS:
        raise ValueError(
            f"Invalid file name {name!r}: the extension must be one of {', '.join(FILE_FORMATS)}."
        )
    validate_identifier(stem, "file name", allow_leading_underscore=False)
    return name


#: Words that are upper-cased rather than capitalised when a name is turned into a label, so a
#: grid header reads "Annual Budget GBP" rather than "Annual Budget Gbp". Only add a word here
#: when it is an acronym in every context: "id" is one, "it" would not be.
ACRONYMS = frozenset(
    {"api", "csv", "csat", "eur", "fte", "fx", "gbp", "gl", "hr", "id", "nps", "uc", "url", "usd", "vat"}
)


def humanize(name: str) -> str:
    """``cost_centre_code`` -> ``Cost Centre Code``; ``customer__survey`` -> ``Customer / Survey``."""
    parts = [p for p in name.split("__")]
    words = [
        " ".join(w.upper() if w in ACRONYMS else w.capitalize() for w in p.split("_") if w) for p in parts
    ]
    return " / ".join(w for w in words if w) or name


def new_row_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------------------
# Types and roles
# --------------------------------------------------------------------------------------


class DataType(enum.StrEnum):
    """Portable column types. Backends map them to native types."""

    STRING = "STRING"
    INTEGER = "INTEGER"
    DECIMAL = "DECIMAL"
    DOUBLE = "DOUBLE"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    TIMESTAMP = "TIMESTAMP"
    OTHER = "OTHER"  # native type not supported by the app; shown read-only

    @property
    def label(self) -> str:
        return _TYPE_LABELS[self]

    @classmethod
    def editable_types(cls) -> list[DataType]:
        return [t for t in cls if t is not cls.OTHER]

    @classmethod
    def from_label(cls, label: str) -> DataType:
        for t, lbl in _TYPE_LABELS.items():
            if lbl == label or t.value == label:
                return t
        raise ValueError(f"Unknown data type label {label!r}")


_TYPE_LABELS = {
    DataType.STRING: "Text",
    DataType.INTEGER: "Whole number",
    DataType.DECIMAL: "Decimal",
    DataType.DOUBLE: "Floating point",
    DataType.BOOLEAN: "Yes / No",
    DataType.DATE: "Date",
    DataType.TIMESTAMP: "Date & time",
    DataType.OTHER: "Other (read-only)",
}

DEFAULT_DECIMAL_PRECISION = 18
DEFAULT_DECIMAL_SCALE = 4


class Role(enum.IntEnum):
    """Access tier for a function (schema). Ordered so that ``role >= Role.EDITOR`` reads naturally."""

    NONE = 0
    VIEWER = 1
    EDITOR = 2
    ADMIN = 3

    @property
    def label(self) -> str:
        return {
            Role.NONE: "No access",
            Role.VIEWER: "Viewer",
            Role.EDITOR: "Editor",
            Role.ADMIN: "Function admin",
        }[self]

    @property
    def can_view(self) -> bool:
        return self >= Role.VIEWER

    @property
    def can_edit(self) -> bool:
        return self >= Role.EDITOR

    @property
    def can_admin(self) -> bool:
        return self >= Role.ADMIN

    @classmethod
    def from_name(cls, name: str) -> Role:
        """Resolve a stored role name, failing *closed* on anything unknown.

        Roles are persisted by name, so a grant written by a build that knows a role this one
        does not (an approver, a contributor) must not be able to break permission resolution
        for everybody. An unrecognised name grants nothing.
        """
        try:
            return cls[str(name).strip().upper()]
        except KeyError:
            return cls.NONE


@dataclass(frozen=True)
class User:
    """The signed-in identity. Groups drive authorisation; roles are resolved by the backend."""

    username: str
    display_name: str = ""
    groups: tuple[str, ...] = ()
    email: str | None = None

    @property
    def principals(self) -> frozenset[str]:
        """Every principal name this user acts as (username, email and groups)."""
        names = {self.username, *self.groups}
        if self.email:
            names.add(self.email)
        return frozenset(names)

    @property
    def label(self) -> str:
        return self.display_name or self.username


GLOBAL_ADMIN_LABEL = "Global admin"


@dataclass
class Permissions:
    """Effective access of one user, as resolved by the backend.

    * ``function_roles`` - role per function (schema). ``Role.ADMIN`` is a *function admin*:
      creates and administers forms inside that function and grants roles on it.
    * ``is_global_admin`` - catalog-level rights: create and delete functions, delete forms,
      administer the domain list, see the technical documentation. In Unity Catalog this is
      CREATE SCHEMA / MANAGE on the catalog (or catalog ownership).
    """

    function_roles: dict[str, Role] = field(default_factory=dict)
    is_global_admin: bool = False

    @property
    def can_create_function(self) -> bool:
        return self.is_global_admin

    @property
    def can_manage_domains(self) -> bool:
        """Only global admins maintain the list of domains (the classifier above functions)."""
        return self.is_global_admin

    @property
    def can_delete(self) -> bool:
        """Deleting functions (schemas) and forms (tables) is reserved to global admins."""
        return self.is_global_admin

    def role_for(self, function: str) -> Role:
        return self.function_roles.get(function, Role.NONE)

    @property
    def visible_functions(self) -> list[str]:
        return sorted(f for f, r in self.function_roles.items() if r.can_view)

    @property
    def admin_functions(self) -> list[str]:
        return sorted(f for f, r in self.function_roles.items() if r.can_admin)

    @property
    def is_admin_anywhere(self) -> bool:
        return self.is_global_admin or any(r.can_admin for r in self.function_roles.values())

    @property
    def summary(self) -> str:
        """Short human description, e.g. 'Global admin' or 'Function admin of 2, editor of 1'."""
        if self.is_global_admin:
            return GLOBAL_ADMIN_LABEL
        counts = {}
        for r in self.function_roles.values():
            if r.can_view:
                counts[r] = counts.get(r, 0) + 1
        if not counts:
            return "No access yet"
        parts = [
            f"{r.label.lower()} of {n} function{'s' if n != 1 else ''}"
            for r, n in sorted(counts.items(), key=lambda x: -x[0])
        ]
        return ", ".join(parts).capitalize()


# --------------------------------------------------------------------------------------
# Persisted metadata contract
# --------------------------------------------------------------------------------------

#: Version of the metadata this build writes: the JSON documents (``rdm.column_config``,
#: ``rdm.settings``) and the shape of the ``_catalog`` registry tables.
#:
#: The contract is **additive**. A new rule or setting is a new key, never a changed or
#: removed one, so the version only has to move when a reader must behave *differently* -
#: which is rare, because every reader keeps the keys it does not understand
#: (:func:`parse_document`) and writes them back untouched (:func:`build_document`).
#: That is what lets two app versions, or the app and an external tool, share one catalog:
#: an older build editing a form cannot silently strip what a newer build stored.
METADATA_VERSION = 1


def parse_document(
    raw: str | dict[str, Any] | None, known: frozenset[str]
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    """Split a persisted JSON document into ``(version, known keys, unknown keys)``.

    Garbage (unparsable text, a non-object, a missing document) yields empty dictionaries
    rather than an error: metadata must never make a form unopenable.
    """
    if not raw:
        return METADATA_VERSION, {}, {}
    try:
        doc = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return METADATA_VERSION, {}, {}
    if not isinstance(doc, dict):
        return METADATA_VERSION, {}, {}
    try:
        version = max(1, int(doc.get("version", METADATA_VERSION)))
    except (TypeError, ValueError):
        version = METADATA_VERSION
    fields = {k: v for k, v in doc.items() if k in known}
    unknown = {k: v for k, v in doc.items() if k not in known and k != "version"}
    return version, fields, unknown


def build_document(version: int, fields: dict[str, Any], unknown: dict[str, Any]) -> dict[str, Any]:
    """Inverse of :func:`parse_document`: unknown keys first, known keys win, version last.

    ``version`` is the one the document was read with, so a document written by a newer
    build keeps its own version through a round trip here.
    """
    doc: dict[str, Any] = {k: v for k, v in unknown.items() if k != "version"}
    doc.update(fields)
    doc["version"] = max(METADATA_VERSION, version)
    return doc


#: Size budget for one persisted metadata document. A metadata document is stored as a single
#: table property, and property values are bounded (the limit is metastore-dependent and has
#: historically been a few kilobytes), so the app keeps its own conservative ceiling: it can
#: then refuse in the user's terms instead of letting the ``ALTER TABLE`` fail - which would
#: also lose the unrelated metadata set by the same statement. Only *writes* are checked; a
#: document that is already too large still loads, it just cannot grow.
MAX_DOCUMENT_BYTES = 4000


def document_json(doc: dict[str, Any], what: str = "configuration") -> str:
    """Compact, key-sorted JSON so an unchanged document produces an unchanged property."""
    text = json.dumps(doc, separators=(",", ":"), sort_keys=True, default=str)
    size = len(text.encode("utf-8"))
    if size > MAX_DOCUMENT_BYTES:
        raise ValueError(
            f"The {what} of this form is too large to store ({size:,} bytes, maximum "
            f"{MAX_DOCUMENT_BYTES:,}). Shorten the longest allowed-value lists, or keep such a "
            f"list in its own form and reference it from the column description."
        )
    return text


# --------------------------------------------------------------------------------------
# Metadata objects
# --------------------------------------------------------------------------------------


@dataclass
class ColumnDef:
    name: str
    data_type: DataType = DataType.STRING
    description: str = ""
    nullable: bool = True
    precision: int | None = None
    scale: int | None = None
    options: list[str] = field(default_factory=list)  # allowed values (renders as a dropdown)
    is_key: bool = False  # business key: combination must be unique
    native_type: str = ""  # type text reported by the backend, informative only
    position: int = 0
    #: Column-config keys this build does not understand (written by a newer build or by a
    #: tool), kept verbatim so saving the form does not discard them. See METADATA_VERSION.
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_system(self) -> bool:
        return is_system_column(self.name)

    @property
    def required(self) -> bool:
        return not self.nullable

    @property
    def type_label(self) -> str:
        if self.data_type is DataType.DECIMAL:
            p, s = self.decimal_params
            return f"DECIMAL({p},{s})"
        if self.data_type is DataType.OTHER and self.native_type:
            return self.native_type
        return self.data_type.value

    @property
    def decimal_params(self) -> tuple[int, int]:
        p = self.precision or DEFAULT_DECIMAL_PRECISION
        s = DEFAULT_DECIMAL_SCALE if self.scale is None else self.scale
        return p, s

    def validate(self) -> ColumnDef:
        validate_identifier(self.name, "column name", allow_leading_underscore=self.is_system)
        if self.data_type is DataType.DECIMAL:
            p, s = self.decimal_params
            if not (1 <= p <= 38) or not (0 <= s <= p):
                raise ValueError(
                    f"Column {self.name!r}: DECIMAL precision must be 1-38 and scale 0-precision."
                )
        if self.options and self.data_type is not DataType.STRING:
            raise ValueError(f"Column {self.name!r}: allowed values are only supported for text columns.")
        return self


def system_columns() -> list[ColumnDef]:
    """The system columns added to every form created by the app, in order."""
    return [
        ColumnDef(ID_COLUMN, DataType.STRING, "Row identifier (generated)", nullable=False),
        ColumnDef(
            VERSION_COLUMN,
            DataType.INTEGER,
            "Row version, incremented on every change (concurrency control)",
            nullable=False,
        ),
        ColumnDef(CREATED_AT_COLUMN, DataType.TIMESTAMP, "Created at"),
        ColumnDef(CREATED_BY_COLUMN, DataType.STRING, "Created by"),
        ColumnDef(UPDATED_AT_COLUMN, DataType.TIMESTAMP, "Last modified at"),
        ColumnDef(UPDATED_BY_COLUMN, DataType.STRING, "Last modified by"),
    ]


#: Functions that have not been assigned to a domain are grouped under this label.
UNASSIGNED_DOMAIN_LABEL = "Unassigned"


@dataclass
class DomainDef:
    """A domain: the top of the hierarchy, a business classifier that groups functions.

    Domains mirror the organisation's data domains (the Databricks domain classification);
    they are a registry entry maintained by global admins, not a Unity Catalog securable.
    """

    name: str
    display_name: str = ""
    description: str = ""
    owner: str = ""
    function_count: int | None = None

    @property
    def title(self) -> str:
        return self.display_name or humanize(self.name)

    def validate(self) -> DomainDef:
        validate_identifier(self.name, "domain name", allow_leading_underscore=False)
        return self


@dataclass
class FunctionDef:
    """A function: one Unity Catalog schema holding the forms of a business function.

    Every function belongs to at most one domain (``domain`` is the domain name, empty when
    the function has not been assigned yet).
    """

    name: str
    display_name: str = ""
    description: str = ""
    owner: str = ""
    owner_email: str = ""  # optional contact e-mail of the owning team or person
    doc_link: str = ""  # project documentation URL
    domain: str = ""  # name of the domain the function is assigned to
    form_count: int | None = None
    file_count: int | None = None
    properties: dict[str, str] = field(default_factory=dict)

    @property
    def title(self) -> str:
        return self.display_name or humanize(self.name)

    def validate(self) -> FunctionDef:
        validate_identifier(self.name, "function name", allow_leading_underscore=False)
        if self.domain:
            validate_identifier(self.domain, "domain name", allow_leading_underscore=False)
        if self.doc_link and not self.doc_link.lower().startswith(("http://", "https://")):
            raise ValueError("The documentation link must start with http:// or https://")
        return self


#: Keys used in table properties / the local property store.
PROP_FORM = "rdm.form"
PROP_DISPLAY_NAME = "rdm.display_name"
PROP_OWNER = "rdm.owner"
PROP_OWNER_EMAIL = "rdm.owner_email"
PROP_COLUMN_CONFIG = "rdm.column_config"
PROP_SETTINGS = "rdm.settings"
PROP_DOC_LINK = "rdm.doc_link"
PROP_DOMAIN = "rdm.domain"
PROP_SCD2 = "rdm.scd2"
TAG_DISPLAY_NAME = "rdm_display_name"
TAG_OWNER = "rdm_owner"
TAG_FORM = "rdm_form"
TAG_DOMAIN = "rdm_domain"

#: Top-level keys of ``rdm.column_config`` this build understands.
COLUMN_CONFIG_KEYS = frozenset({"columns"})
#: Keys of one column's entry in ``rdm.column_config`` this build understands.
COLUMN_RULE_KEYS = frozenset({"options", "key"})
#: Keys of ``rdm.settings`` this build interprets. Empty today: the document exists so the
#: first per-form option (an approval step, a retention rule, a notification) is a change to
#: this set and to the UI, and to nothing in the backends.
SETTINGS_KEYS: frozenset[str] = frozenset()

#: SCD Type 2 history tables use the organisation's standard notation (Databricks Auto CDC):
#: a validity window per version, the current row has ``__END_AT IS NULL``.
SCD2_START_COLUMN = "__START_AT"
SCD2_END_COLUMN = "__END_AT"
SCD2_TABLE_PREFIX = "_h__"


def scd2_table_name(form_name: str) -> str:
    """History table of a form: app-managed (leading ``_``), lives in the same schema."""
    name = f"{SCD2_TABLE_PREFIX}{form_name}"
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise ValueError(
            f"The form name is too long for a history table ({len(name)} > {MAX_IDENTIFIER_LENGTH} "
            f"characters with the '{SCD2_TABLE_PREFIX}' prefix)."
        )
    return name


@dataclass
class FormDef:
    function: str  # the function (schema) the form lives in
    name: str
    display_name: str = ""
    description: str = ""
    owner: str = ""
    owner_email: str = ""  # optional contact e-mail of the owning team or person
    scd2_enabled: bool = False  # FR-47: maintain a Type 2 history table (__START_AT/__END_AT)
    columns: list[ColumnDef] = field(default_factory=list)
    properties: dict[str, str] = field(default_factory=dict)
    tags: dict[str, str] = field(default_factory=dict)
    row_count: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    updated_by: str = ""
    #: Per-form settings that are neither a column rule nor a description (``rdm.settings``).
    #: Free-form on purpose: this is where the next per-form option lands as an additive
    #: change here, without a new property key, a backend change or a migration.
    settings: dict[str, Any] = field(default_factory=dict)
    settings_version: int = METADATA_VERSION
    #: Version and unknown top-level keys of ``rdm.column_config``, preserved on write.
    config_version: int = METADATA_VERSION
    config_extra: dict[str, Any] = field(default_factory=dict)

    @property
    def full_name(self) -> str:
        return f"{self.function}.{self.name}"

    @property
    def title(self) -> str:
        return self.display_name or humanize(self.name)

    @property
    def user_columns(self) -> list[ColumnDef]:
        return [c for c in self.columns if not c.is_system]

    @property
    def key_columns(self) -> list[ColumnDef]:
        return [c for c in self.user_columns if c.is_key]

    @property
    def has_system_columns(self) -> bool:
        names = {c.name for c in self.columns}
        return ID_COLUMN in names and VERSION_COLUMN in names

    @property
    def is_editable(self) -> bool:
        """Only tables with the app's system columns can be edited row by row safely."""
        return self.has_system_columns

    def column(self, name: str) -> ColumnDef | None:
        for c in self.columns:
            if c.name == name:
                return c
        return None

    def validate(self) -> FormDef:
        validate_identifier(self.function, "function name", allow_leading_underscore=False)
        validate_identifier(self.name, "form name", allow_leading_underscore=False)
        seen: set[str] = set()
        for c in self.columns:
            c.validate()
            if c.name in seen:
                raise ValueError(f"Duplicate column name {c.name!r}.")
            seen.add(c.name)
        if not self.user_columns:
            raise ValueError("A form needs at least one column.")
        return self

    # -- configuration persisted as JSON properties ----------------------------------------
    #
    # Two documents per form, both following the additive contract of METADATA_VERSION:
    # ``rdm.column_config`` holds the per-column rules SQL cannot express (allowed values,
    # business keys), ``rdm.settings`` holds per-form options. They are separate properties
    # so that a form with long allowed-value lists cannot crowd its settings out of the
    # property's size budget.

    def column_config(self) -> dict[str, Any]:
        cols: dict[str, Any] = {}
        for c in self.user_columns:
            entry: dict[str, Any] = {k: v for k, v in c.extra.items() if k not in COLUMN_RULE_KEYS}
            if c.options:
                entry["options"] = list(c.options)
            if c.is_key:
                entry["key"] = True
            if entry:
                cols[c.name] = entry
        return build_document(self.config_version, {"columns": cols}, self.config_extra)

    def column_config_json(self) -> str:
        return document_json(self.column_config(), "column configuration")

    def apply_column_config(self, raw: str | dict[str, Any] | None) -> None:
        """Merge the persisted column rules back onto ``self.columns``. Ignores garbage.

        Keys this build knows become model fields (``options``, ``key``); every other key is
        kept on the column (:attr:`ColumnDef.extra`) or on the form (:attr:`config_extra`) so
        that writing the form back preserves what a newer build stored.
        """
        self.config_version, fields, self.config_extra = parse_document(raw, COLUMN_CONFIG_KEYS)
        cols = fields.get("columns")
        if not isinstance(cols, dict):
            cols = {}
        for c in self.columns:
            entry = cols.get(c.name)
            if not isinstance(entry, dict):
                c.extra = {}
                continue
            opts = entry.get("options")
            if isinstance(opts, list):
                c.options = [str(o) for o in opts]
            c.is_key = bool(entry.get("key", False))
            c.extra = {k: v for k, v in entry.items() if k not in COLUMN_RULE_KEYS}

    def settings_document(self) -> dict[str, Any]:
        return build_document(self.settings_version, {}, self.settings)

    def settings_json(self) -> str:
        return document_json(self.settings_document(), "settings")

    def apply_settings(self, raw: str | dict[str, Any] | None) -> None:
        """Load ``rdm.settings``. Every key is preserved; none is interpreted yet."""
        self.settings_version, _, self.settings = parse_document(raw, SETTINGS_KEYS)


@dataclass
class FileDef:
    """A file: a CSV or Parquet dataset in a function's volume, for lists too large for a grid.

    Files share the function's access rules and metadata (display name, description, owner,
    registry entry, history of uploads) but are not edited row by row: they are uploaded,
    previewed, downloaded and replaced as a whole.
    """

    function: str
    name: str  # ``<identifier>.<csv|parquet>``, the file name in the volume
    display_name: str = ""
    description: str = ""
    owner: str = ""
    owner_email: str = ""  # optional contact e-mail of the owning team or person
    size_bytes: int | None = None
    row_count: int | None = None
    path: str = ""  # where the backend stores it (volume path or local path), informative
    registered: bool = True  # False for a file found in storage without a registry entry
    created_at: datetime | None = None
    created_by: str = ""
    updated_at: datetime | None = None
    updated_by: str = ""

    @property
    def stem(self) -> str:
        return split_file_name(self.name)[0]

    @property
    def format(self) -> str:
        return split_file_name(self.name)[1]

    @property
    def full_name(self) -> str:
        return f"{self.function}/{self.name}"

    @property
    def title(self) -> str:
        return self.display_name or humanize(self.stem)

    def validate(self) -> FileDef:
        validate_identifier(self.function, "function name", allow_leading_underscore=False)
        validate_file_name(self.name)
        return self


#: Change types recorded for files in the audit trail.
FILE_CHANGE_TYPES: tuple[str, ...] = ("upload", "replace", "delete")


# --------------------------------------------------------------------------------------
# Change tracking
# --------------------------------------------------------------------------------------


@dataclass
class RowInsert:
    values: dict[str, Any]
    label: str = ""  # human label used in validation messages ("new row 2")


@dataclass
class RowUpdate:
    row_id: str
    changes: dict[str, Any]
    expected_version: int | None = None  # ``_version`` seen when the row was loaded
    label: str = ""


@dataclass
class RowDelete:
    row_id: str
    expected_version: int | None = None
    label: str = ""


@dataclass
class ChangeSet:
    inserts: list[RowInsert] = field(default_factory=list)
    updates: list[RowUpdate] = field(default_factory=list)
    deletes: list[RowDelete] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.inserts or self.updates or self.deletes)

    @property
    def total(self) -> int:
        return len(self.inserts) + len(self.updates) + len(self.deletes)

    def summary(self) -> str:
        parts = []
        if self.inserts:
            parts.append(f"{len(self.inserts)} added")
        if self.updates:
            parts.append(f"{len(self.updates)} edited")
        if self.deletes:
            parts.append(f"{len(self.deletes)} deleted")
        return ", ".join(parts) if parts else "no changes"


@dataclass
class ValidationIssue:
    row_label: str
    column: str | None
    message: str
    row_id: str | None = None  # set when the issue can be tied to a specific row

    def __str__(self) -> str:
        where = f"{self.row_label}, column '{self.column}'" if self.column else self.row_label
        return f"{where}: {self.message}"


@dataclass
class SaveResult:
    inserted: int = 0
    updated: int = 0
    deleted: int = 0
    conflicts: list[str] = field(default_factory=list)  # labels of rows changed by someone else
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.conflicts

    @property
    def applied(self) -> int:
        return self.inserted + self.updated + self.deleted

    def summary(self) -> str:
        parts = []
        if self.inserted:
            parts.append(f"{self.inserted} added")
        if self.updated:
            parts.append(f"{self.updated} updated")
        if self.deleted:
            parts.append(f"{self.deleted} deleted")
        text = ", ".join(parts) if parts else "nothing changed"
        if self.conflicts:
            text += f"; {len(self.conflicts)} row(s) skipped because they were changed by someone else"
        return text
