"""Grid editing: value coercion, change-set building, validation and guarded persistence.

``st.data_editor`` reports edits positionally (``edited_rows`` keyed by row position,
``added_rows`` and ``deleted_rows``). :func:`build_changeset` resolves those positions
against the DataFrame that was displayed (the *snapshot*) to stable row identifiers and
produces a validated :class:`~rdm.models.ChangeSet` the backend can apply.
"""

from __future__ import annotations

import logging
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
    FormDef,
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
    """Normalised copy of ``st.session_state[<data_editor key>]``."""

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
    seen: dict[tuple, str] = {}
    issues: list[ValidationIssue] = []

    def key_of(values: Mapping[str, Any]) -> tuple:
        return tuple(_norm_key(values.get(k)) for k in keys)

    if not snapshot.empty and ID_COLUMN in snapshot.columns:
        for rec in snapshot[[ID_COLUMN, *[k for k in keys if k in snapshot.columns]]].to_dict("records"):
            rid = str(rec[ID_COLUMN])
            if rid in deleted:
                continue
            merged = {**rec, **updates.get(rid, {})}
            key = key_of(merged)
            if all(k is None for k in key):
                continue
            label = describe_row(form, merged)
            if key in seen and rid in updates:
                issues.append(ValidationIssue(label, ", ".join(keys), f"duplicates existing {seen[key]}"))
            seen.setdefault(key, describe_row(form, rec))
    for ins in changes.inserts:
        key = key_of(ins.values)
        if all(k is None for k in key):
            continue
        if key in seen:
            issues.append(ValidationIssue(ins.label, ", ".join(keys), f"duplicates {seen[key]}"))
        else:
            seen[key] = ins.label
    return issues


def _norm_key(value: Any) -> Any:
    if is_missing(value):
        return None
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------------------
# Guarded service facade
# --------------------------------------------------------------------------------------


class FormService:
    """Everything the UI does with forms, with role checks in front of the backend."""

    def __init__(self, backend: DatabaseBackend, user: User, permissions: Permissions) -> None:
        self.backend = backend
        self.user = user
        self.permissions = permissions

    # -- guards ----------------------------------------------------------------------------

    def role(self, domain: str) -> Role:
        return self.permissions.role_for(domain)

    def require(self, domain: str, role: Role) -> None:
        have = self.role(domain)
        if have < role:
            raise PermissionDenied(
                f"{role.label} access to domain '{domain}' is required (you have: {have.label})."
            )

    # -- reading ---------------------------------------------------------------------------

    def get_form(self, domain: str, name: str) -> FormDef:
        self.require(domain, Role.VIEWER)
        return self.backend.get_form(domain, name)

    def load_rows(
        self,
        form: FormDef,
        search: str | None = None,
        limit: int = 5000,
        order_by: str | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        self.require(form.domain, Role.VIEWER)
        if order_by and form.column(order_by) is None:
            order_by = None
        return self.backend.read_rows(
            form, search=search or None, limit=limit, order_by=order_by, descending=descending
        )

    def history(self, form: FormDef, limit: int = 200) -> pd.DataFrame:
        self.require(form.domain, Role.VIEWER)
        return self.backend.get_history(form, limit=limit)

    # -- editing ---------------------------------------------------------------------------

    def save(self, form: FormDef, changes: ChangeSet) -> SaveResult:
        self.require(form.domain, Role.EDITOR)
        if changes.is_empty:
            return SaveResult()
        log.info("%s saving %s on %s", self.user.username, changes.summary(), form.full_name)
        return self.backend.apply_changes(form, changes, self.user)

    def append_rows(self, form: FormDef, rows: pd.DataFrame) -> int:
        self.require(form.domain, Role.EDITOR)
        return self.backend.append_rows(form, rows, self.user)

    # -- administration --------------------------------------------------------------------

    def create_form(self, form: FormDef, rows: pd.DataFrame | None = None) -> FormDef:
        self.require(form.domain, Role.ADMIN)
        return self.backend.create_form(form, self.user, rows)

    def update_form_metadata(self, form: FormDef) -> FormDef:
        self.require(form.domain, Role.ADMIN)
        return self.backend.update_form_metadata(form, self.user)

    def add_column(self, form: FormDef, column: ColumnDef) -> FormDef:
        self.require(form.domain, Role.ADMIN)
        return self.backend.add_column(form, column, self.user)

    def drop_column(self, form: FormDef, column_name: str) -> FormDef:
        self.require(form.domain, Role.ADMIN)
        return self.backend.drop_column(form, column_name, self.user)

    def drop_form(self, form: FormDef) -> None:
        self.require(form.domain, Role.ADMIN)
        self.backend.drop_form(form, self.user)

    def create_domain(self, domain: DomainDef) -> DomainDef:
        if not self.permissions.can_create_domain:
            raise PermissionDenied("Creating domains requires catalog administrator rights.")
        return self.backend.create_domain(domain, self.user)

    def update_domain(self, domain: DomainDef) -> DomainDef:
        self.require(domain.name, Role.ADMIN)
        return self.backend.update_domain(domain, self.user)

    def list_domain_grants(self, domain: str) -> list[tuple[str, Role]]:
        self.require(domain, Role.ADMIN)
        return self.backend.list_domain_grants(domain)

    def grant_domain_role(self, domain: str, principal: str, role: Role) -> None:
        self.require(domain, Role.ADMIN)
        self.backend.grant_domain_role(domain, principal, role, self.user)
