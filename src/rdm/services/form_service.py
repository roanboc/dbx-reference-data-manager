"""Grid editing: value coercion, change-set building, validation and guarded persistence.

:func:`build_changeset` resolves positional editor state (``edited_rows`` keyed by row
position, ``added_rows`` and ``deleted_rows``) against the DataFrame that was displayed (the
*snapshot*) to stable row identifiers and produces a validated
:class:`~rdm.models.ChangeSet` the backend can apply. The Dash grid uses the row-identity
based :mod:`rdm.services.draft` instead; :class:`FormService` guards both.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from rdm.backend.base import DatabaseBackend, PermissionDenied
from rdm.coercion import (  # noqa: F401 - re-exported
    FALSE_WORDS,
    TRUE_WORDS,
    CoercionError,
    coerce_value,
    is_missing,
)
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    RowDelete,
    RowInsert,
    RowUpdate,
    SaveResult,
    User,
    ValidationIssue,
)

log = logging.getLogger(__name__)


@dataclass
class EditorState:
    """Normalised positional editor state (``edited_rows``, ``added_rows``, ``deleted_rows``)."""

    edited_rows: dict[int, dict[str, Any]] = field(default_factory=dict)
    added_rows: list[dict[str, Any]] = field(default_factory=list)
    deleted_rows: list[int] = field(default_factory=list)

    @classmethod
    def from_session(cls, raw: Mapping[str, Any] | None) -> EditorState:
        if not raw:
            return cls()
        edited = {int(k): dict(v) for k, v in (raw.get("edited_rows") or {}).items()}
        added = [dict(r) for r in (raw.get("added_rows") or [])]
        deleted = [int(i) for i in (raw.get("deleted_rows") or [])]
        return cls(edited, added, deleted)

    @property
    def is_empty(self) -> bool:
        return not (self.edited_rows or self.added_rows or self.deleted_rows)


def describe_row(form: FormDef, values: Mapping[str, Any], fallback: str = "row") -> str:
    """Human label for a row: its business key(s), else the first text value."""
    keys = form.key_columns
    if keys:
        parts = [f"{k.name}={_short(values.get(k.name))}" for k in keys]
        return "Row " + ", ".join(parts)
    for c in form.user_columns:
        if c.data_type is DataType.STRING and not is_missing(values.get(c.name)):
            return f"Row '{_short(values.get(c.name))}'"
    rid = values.get(ID_COLUMN)
    return f"{fallback} {str(rid)[:8]}" if rid else fallback


def _short(value: Any, n: int = 40) -> str:
    s = "" if is_missing(value) else str(value)
    return s if len(s) <= n else s[: n - 1] + "…"


def build_changeset(
    form: FormDef, snapshot: pd.DataFrame, state: EditorState
) -> tuple[ChangeSet, list[ValidationIssue]]:
    """Resolve positional editor state against ``snapshot`` and validate."""
    changes = ChangeSet()
    issues: list[ValidationIssue] = []
    user_cols = {c.name: c for c in form.user_columns}
    deleted_positions = set(state.deleted_rows)
    n = len(snapshot)

    def coerce_into(target: dict[str, Any], col: ColumnDef, raw: Any, label: str) -> None:
        try:
            value = coerce_value(col, raw)
        except CoercionError as exc:
            issues.append(ValidationIssue(label, col.name, str(exc)))
            return
        if value is None and col.required:
            issues.append(ValidationIssue(label, col.name, "is required"))
        if value is not None and col.options and str(value) not in col.options:
            issues.append(ValidationIssue(label, col.name, f"must be one of: {', '.join(col.options)}"))
        target[col.name] = value

    # Updates
    for pos, edits in sorted(state.edited_rows.items()):
        if pos in deleted_positions or pos < 0 or pos >= n:
            continue
        row = snapshot.iloc[pos]
        label = describe_row(form, row.to_dict(), fallback=f"row {pos + 1}")
        values: dict[str, Any] = {}
        for col_name, raw in edits.items():
            col = user_cols.get(col_name)
            if col is None:
                continue  # system or unknown column: ignore silently
            if col.data_type is DataType.OTHER:
                issues.append(ValidationIssue(label, col_name, "column is read-only"))
                continue
            coerce_into(values, col, raw, label)
        if values:
            changes.updates.append(RowUpdate(str(row[ID_COLUMN]), values, _version_of(row), label))

    # Inserts
    for i, added in enumerate(state.added_rows):
        label = f"New row {i + 1}"
        values = {}
        for col in user_cols.values():
            if col.data_type is DataType.OTHER:
                continue
            coerce_into(values, col, added.get(col.name), label)
        if all(v is None for v in values.values()):
            issues.append(ValidationIssue(label, None, "is empty"))
        changes.inserts.append(RowInsert(values, label))

    # Deletes
    for pos in sorted(deleted_positions):
        if pos < 0 or pos >= n:
            continue
        row = snapshot.iloc[pos]
        label = describe_row(form, row.to_dict(), fallback=f"row {pos + 1}")
        changes.deletes.append(RowDelete(str(row[ID_COLUMN]), _version_of(row), label))

    issues.extend(_check_unique_keys(form, snapshot, changes))
    return changes, issues


def _version_of(row: Mapping[str, Any]) -> int | None:
    v = row.get(VERSION_COLUMN)
    return None if is_missing(v) else int(v)


def _check_unique_keys(form: FormDef, snapshot: pd.DataFrame, changes: ChangeSet) -> list[ValidationIssue]:
    """Business-key uniqueness across the loaded rows after applying the change set."""
    keys = [c.name for c in form.key_columns]
    if not keys or snapshot.empty and not changes.inserts:
        return []
    deleted = {d.row_id for d in changes.deletes}
    updates = {u.row_id: u.changes for u in changes.updates}
    column_label = ", ".join(keys)
    issues: list[ValidationIssue] = []

    def key_of(values: Mapping[str, Any]) -> tuple:
        return tuple(_norm_key(values.get(k)) for k in keys)

    # Every surviving row under the key it will have *after* the save. Grouping first rather
    # than checking as we go matters: an edited row can collide with a row further down the
    # snapshot, which a single forward pass never sees.
    by_key: dict[tuple, list[tuple[str, str, bool]]] = {}  # key -> [(row id, label, changed)]
    if not snapshot.empty and ID_COLUMN in snapshot.columns:
        for rec in snapshot[[ID_COLUMN, *[k for k in keys if k in snapshot.columns]]].to_dict("records"):
            rid = str(rec[ID_COLUMN])
            if rid in deleted:
                continue
            merged = {**rec, **updates.get(rid, {})}
            key = key_of(merged)
            if all(k is None for k in key):
                continue
            by_key.setdefault(key, []).append((rid, describe_row(form, merged), rid in updates))

    for entries in by_key.values():
        if len(entries) < 2:
            continue
        # Only rows this save touched are the user's to fix: a duplicate that was already in
        # the table is reported when someone edits it, not the moment the form is opened.
        for rid, label, changed in entries:
            if not changed:
                continue
            other = next(lbl for other_rid, lbl, _ in entries if other_rid != rid)
            issues.append(ValidationIssue(label, column_label, f"duplicates existing {other}"))

    for ins in changes.inserts:
        key = key_of(ins.values)
        if all(k is None for k in key):
            continue
        existing = by_key.get(key)
        if existing:
            issues.append(ValidationIssue(ins.label, column_label, f"duplicates {existing[0][1]}"))
        else:
            by_key[key] = [("", ins.label, True)]
    return issues


def _norm_key(value: Any) -> Any:
    if is_missing(value):
        return None
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def require_metadata(kind: str, description: str | None, owner: str | None, owner_email: str | None = "") -> None:
    """FR-40/FR-41: a description and an owner are mandatory; the contact e-mail is optional."""
    missing = []
    if not (description or "").strip():
        missing.append("a description")
    if not (owner or "").strip():
        missing.append("an owner (a team or a person)")
    if missing:
        raise ValueError(f"The {kind} needs {' and '.join(missing)}.")
    email = (owner_email or "").strip()
    if email and not _EMAIL_RE.fullmatch(email):
        raise ValueError(f"'{email}' does not look like an e-mail address.")


# --------------------------------------------------------------------------------------
# Import modes (FR-43): merge / replace an imported frame by business key
# --------------------------------------------------------------------------------------

IMPORT_MODES = ("append", "merge", "replace")


def build_import_changeset(
    form: FormDef, current: pd.DataFrame, incoming: pd.DataFrame, mode: str
) -> tuple[ChangeSet, list[str]]:
    """Resolve an imported (already coerced) frame against the current rows by business key.

    ``merge`` updates matched rows and inserts new ones; ``replace`` additionally deletes
    current rows whose key is not in the file. Only columns present in the file are
    touched. The result is saved through :meth:`FormService.save`, so audit and
    optimistic concurrency work exactly as for grid edits.
    """
    if mode not in ("merge", "replace"):
        raise ValueError(f"Unknown import mode '{mode}'.")
    keys = [c.name for c in form.key_columns]
    if not keys:
        return ChangeSet(), [
            "Merge and replace need business key columns. Mark the key column(s) on the Schema tab first."
        ]
    key_label = ", ".join(keys)

    def key_of(rec: Mapping[str, Any]) -> tuple:
        return tuple(_norm_key(rec.get(k)) for k in keys)

    problems: list[str] = []
    incoming_records = incoming.to_dict("records")
    seen: dict[tuple, int] = {}
    for i, rec in enumerate(incoming_records, start=2):  # 1-based plus the header row
        k = key_of(rec)
        if all(v is None for v in k):
            problems.append(f"File row {i}: the business key ({key_label}) is empty.")
        elif k in seen:
            problems.append(f"File row {i} repeats the key of file row {seen[k]} ({key_label}).")
        else:
            seen[k] = i
    by_key: dict[tuple, dict[str, Any]] = {}
    duplicate_current = set()
    for rec in current.to_dict("records"):
        k = key_of(rec)
        if k in by_key:
            duplicate_current.add(k)
        else:
            by_key[k] = rec
    if duplicate_current:
        problems.append(
            f"The list itself has {len(duplicate_current)} duplicated key(s) ({key_label}); "
            "fix them in the grid before a merge or replace import."
        )
    if problems:
        return ChangeSet(), problems

    file_columns = set(incoming.columns)
    changes = ChangeSet()
    matched: set[tuple] = set()
    for i, rec in enumerate(incoming_records, start=2):
        label = f"file row {i}"
        cur = by_key.get(key_of(rec))
        if cur is None:
            changes.inserts.append(RowInsert(dict(rec), label=label))
            continue
        matched.add(key_of(rec))
        diffs = {
            col.name: rec.get(col.name)
            for col in form.user_columns
            if col.name in file_columns and not _values_equal(col, rec.get(col.name), cur.get(col.name))
        }
        if diffs:
            changes.updates.append(
                RowUpdate(
                    str(cur[ID_COLUMN]), diffs, expected_version=cur.get(VERSION_COLUMN), label=label
                )
            )
    if mode == "replace":
        for k, cur in by_key.items():
            if k not in matched:
                changes.deletes.append(
                    RowDelete(
                        str(cur[ID_COLUMN]),
                        expected_version=cur.get(VERSION_COLUMN),
                        label=describe_row(form, cur),
                    )
                )
    for ins in changes.inserts:
        for col in form.user_columns:
            value = ins.values.get(col.name)
            if col.required and is_missing(value):
                problems.append(f"{ins.label}: '{col.name}' is required.")
            elif not is_missing(value) and col.options and str(value) not in col.options:
                problems.append(f"{ins.label}: '{col.name}' must be one of: {', '.join(col.options)}.")
    for upd in changes.updates:
        for col in form.user_columns:
            if col.name not in upd.changes:
                continue
            value = upd.changes[col.name]
            if col.required and is_missing(value):
                problems.append(f"{upd.label}: '{col.name}' is required.")
            elif not is_missing(value) and col.options and str(value) not in col.options:
                problems.append(f"{upd.label}: '{col.name}' must be one of: {', '.join(col.options)}.")
    return changes, problems


def _values_equal(col: ColumnDef, a: Any, b: Any) -> bool:
    if is_missing(a) and is_missing(b):
        return True
    if is_missing(a) or is_missing(b):
        return False
    try:
        if col.data_type in (DataType.INTEGER, DataType.DOUBLE, DataType.DECIMAL):
            return float(a) == float(b)
        if col.data_type is DataType.BOOLEAN:
            return bool(a) == bool(b)
        if col.data_type in (DataType.DATE, DataType.TIMESTAMP):
            return pd.Timestamp(a) == pd.Timestamp(b)
    except (TypeError, ValueError):
        return False
    return str(a) == str(b)


# --------------------------------------------------------------------------------------
# Guarded service facade
# --------------------------------------------------------------------------------------


class FormService:
    """Everything the UI does with forms, functions and domains, with role checks in front of the backend."""

    def __init__(self, backend: DatabaseBackend, user: User, permissions: Permissions) -> None:
        self.backend = backend
        self.user = user
        self.permissions = permissions

    # -- guards ----------------------------------------------------------------------------

    def role(self, function: str) -> Role:
        return self.permissions.role_for(function)

    def require(self, function: str, role: Role) -> None:
        have = self.role(function)
        if have < role:
            raise PermissionDenied(
                f"{role.label} access to function '{function}' is required (you have: {have.label})."
            )

    def require_global_admin(self, action: str) -> None:
        if not self.permissions.is_global_admin:
            raise PermissionDenied(f"{action} requires global administrator rights.")

    # -- reading ---------------------------------------------------------------------------

    def get_form(self, function: str, name: str) -> FormDef:
        self.require(function, Role.VIEWER)
        return self.backend.get_form(function, name)

    def load_rows(
        self,
        form: FormDef,
        search: str | None = None,
        limit: int = 5000,
        order_by: str | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        self.require(form.function, Role.VIEWER)
        if order_by and form.column(order_by) is None:
            order_by = None
        return self.backend.read_rows(
            form, search=search or None, limit=limit, order_by=order_by, descending=descending
        )

    def history(self, form: FormDef, limit: int = 200, row_id: str | None = None) -> pd.DataFrame:
        self.require(form.function, Role.VIEWER)
        return self.backend.get_history(form, limit=limit, row_id=row_id)

    # -- editing ---------------------------------------------------------------------------

    def save(self, form: FormDef, changes: ChangeSet) -> SaveResult:
        self.require(form.function, Role.EDITOR)
        if changes.is_empty:
            return SaveResult()
        log.info("%s saving %s on %s", self.user.username, changes.summary(), form.full_name)
        return self.backend.apply_changes(form, changes, self.user)

    def append_rows(self, form: FormDef, rows: pd.DataFrame) -> int:
        self.require(form.function, Role.EDITOR)
        return self.backend.append_rows(form, rows, self.user)

    # -- form administration (function admins) ---------------------------------------------

    def create_form(self, form: FormDef, rows: pd.DataFrame | None = None) -> FormDef:
        self.require(form.function, Role.ADMIN)
        require_metadata("form", form.description, form.owner, form.owner_email)
        return self.backend.create_form(form, self.user, rows)

    def update_form_metadata(self, form: FormDef) -> FormDef:
        self.require(form.function, Role.ADMIN)
        require_metadata("form", form.description, form.owner, form.owner_email)
        return self.backend.update_form_metadata(form, self.user)

    def add_column(self, form: FormDef, column: ColumnDef) -> FormDef:
        self.require(form.function, Role.ADMIN)
        if not (column.description or "").strip():
            raise ValueError(f"Column '{column.name}' needs a description.")
        return self.backend.add_column(form, column, self.user)

    def drop_column(self, form: FormDef, column_name: str) -> FormDef:
        self.require(form.function, Role.ADMIN)
        return self.backend.drop_column(form, column_name, self.user)

    def drop_form(self, form: FormDef) -> None:
        """Deleting a form (dropping its table) is reserved to global admins."""
        self.require_global_admin("Deleting a form")
        log.info("%s dropping form %s", self.user.username, form.full_name)
        self.backend.drop_form(form, self.user)

    def set_scd2(self, form: FormDef, enabled: bool) -> FormDef:
        """FR-47: turn the optional Type 2 history table of a form on or off (function admins)."""
        self.require(form.function, Role.ADMIN)
        log.info("%s sets SCD2 of %s to %s", self.user.username, form.full_name, enabled)
        return self.backend.set_scd2(form, enabled, self.user)

    # -- files (CSV / Parquet in the function's volume) -------------------------------------

    def list_files(self, function: str) -> list[FileDef]:
        self.require(function, Role.VIEWER)
        return self.backend.list_files(function)

    def get_file(self, function: str, name: str) -> FileDef:
        self.require(function, Role.VIEWER)
        return self.backend.get_file(function, name)

    def read_file(self, file: FileDef) -> bytes:
        self.require(file.function, Role.VIEWER)
        return self.backend.read_file(file)

    def preview_file(self, file: FileDef, limit: int = 100) -> pd.DataFrame:
        self.require(file.function, Role.VIEWER)
        return self.backend.preview_file(file, limit=limit)

    def file_columns(self, file: FileDef) -> list[ColumnDef]:
        self.require(file.function, Role.VIEWER)
        return self.backend.file_columns(file)

    def file_history(self, file: FileDef, limit: int = 200) -> pd.DataFrame:
        self.require(file.function, Role.VIEWER)
        return self.backend.file_history(file, limit=limit)

    def add_file(self, file: FileDef, data: bytes) -> FileDef:
        """Adding a file to a function is a function-admin act, like creating a form."""
        self.require(file.function, Role.ADMIN)
        require_metadata("file", file.description, file.owner, file.owner_email)
        log.info("%s adding file %s (%d bytes)", self.user.username, file.full_name, len(data))
        return self.backend.put_file(file, data, self.user, replace=False)

    def replace_file(self, file: FileDef, data: bytes) -> FileDef:
        """Replacing the content of a file is an editor act, like changing rows."""
        self.require(file.function, Role.EDITOR)
        log.info("%s replacing file %s (%d bytes)", self.user.username, file.full_name, len(data))
        return self.backend.put_file(file, data, self.user, replace=True)

    def update_file_metadata(self, file: FileDef) -> FileDef:
        self.require(file.function, Role.ADMIN)
        require_metadata("file", file.description, file.owner, file.owner_email)
        return self.backend.update_file_metadata(file, self.user)

    def drop_file(self, file: FileDef) -> None:
        """Deleting a file is reserved to global admins, like deleting a form."""
        self.require_global_admin("Deleting a file")
        log.info("%s dropping file %s", self.user.username, file.full_name)
        self.backend.drop_file(file, self.user)

    # -- functions (schemas) ---------------------------------------------------------------

    def create_function(self, function: FunctionDef) -> FunctionDef:
        self.require_global_admin("Creating a function")
        require_metadata("function", function.description, function.owner, function.owner_email)
        return self.backend.create_function(function, self.user)

    def update_function(self, function: FunctionDef) -> FunctionDef:
        """Function admins edit the details; moving a function to another domain is a global-admin act."""
        self.require(function.name, Role.ADMIN)
        require_metadata("function", function.description, function.owner, function.owner_email)
        if not self.permissions.is_global_admin:
            current = self.backend.get_function(function.name)
            if (function.domain or "") != (current.domain or ""):
                raise PermissionDenied(
                    "Assigning a function to a domain requires global administrator rights."
                )
        return self.backend.update_function(function, self.user)

    def drop_function(self, function: FunctionDef) -> None:
        """Deleting a function (dropping its schema) is reserved to global admins."""
        self.require_global_admin("Deleting a function")
        log.info("%s dropping function %s", self.user.username, function.name)
        self.backend.drop_function(function, self.user)

    def list_groups(self, query: str | None = None) -> list[str]:
        return self.backend.list_groups(query)

    def list_function_grants(self, function: str) -> list[tuple[str, Role]]:
        self.require(function, Role.ADMIN)
        return self.backend.list_function_grants(function)

    def grant_function_role(self, function: str, principal: str, role: Role) -> None:
        self.require(function, Role.ADMIN)
        self.backend.grant_function_role(function, principal, role, self.user)

    # -- domains (global admins) -----------------------------------------------------------

    def list_domains(self) -> list[DomainDef]:
        """The domain list is public: it groups the navigation for everyone."""
        return self.backend.list_domains()

    def create_domain(self, domain: DomainDef) -> DomainDef:
        self.require_global_admin("Creating a domain")
        return self.backend.create_domain(domain, self.user)

    def update_domain(self, domain: DomainDef) -> DomainDef:
        self.require_global_admin("Editing a domain")
        return self.backend.update_domain(domain, self.user)

    def delete_domain(self, name: str) -> None:
        self.require_global_admin("Deleting a domain")
        self.backend.delete_domain(name, self.user)
