"""Row-identity based edit tracking (used by the Dash grid).

A :class:`Draft` accumulates edits keyed by ``_id`` rather than by row position:

* ``updates``: ``{_id: {"_version": n, col: value, ...}}`` - changed cells of existing rows
* ``inserts``: ``{tmp_id: {col: value, ...}}`` - new rows (``tmp_id`` starts with ``new:``)
* ``deletes``: ``{_id: {"_version": n, "label": "..."}}`` - rows marked for deletion

It is plain data (JSON-serialisable) so it can live in a browser-side store, and it is
converted into the same :class:`~rdm.models.ChangeSet` the backends already accept.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pandas as pd

from rdm.coercion import CoercionError, coerce_value, is_missing, rule_violation
from rdm.models import (
    ID_COLUMN,
    VERSION_COLUMN,
    ChangeSet,
    ColumnDef,
    DataType,
    FormDef,
    RowDelete,
    RowInsert,
    RowUpdate,
    ValidationIssue,
)

NEW_ID_PREFIX = "new:"
NEW_FLAG = "_new"  # marks a pending insert on a grid row (the grid highlights it)


def new_temp_id() -> str:
    return NEW_ID_PREFIX + uuid.uuid4().hex


def is_temp_id(row_id: Any) -> bool:
    return isinstance(row_id, str) and row_id.startswith(NEW_ID_PREFIX)


@dataclass
class Draft:
    updates: dict[str, dict[str, Any]] = field(default_factory=dict)
    inserts: dict[str, dict[str, Any]] = field(default_factory=dict)
    deletes: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> Draft:
        if not raw:
            return cls()
        return cls(
            updates={str(k): dict(v) for k, v in (raw.get("updates") or {}).items()},
            inserts={str(k): dict(v) for k, v in (raw.get("inserts") or {}).items()},
            deletes={str(k): dict(v) for k, v in (raw.get("deletes") or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {"updates": self.updates, "inserts": self.inserts, "deletes": self.deletes}

    @property
    def is_empty(self) -> bool:
        return not (self.updates or self.inserts or self.deletes)

    # -- mutations -------------------------------------------------------------------------

    def set_cell(self, row_id: str, column: str, value: Any, version: Any = None) -> None:
        """Record a cell edit from the grid (``version`` is the row's ``_version``)."""
        if is_temp_id(row_id):
            self.inserts.setdefault(row_id, {})[column] = value
            return
        entry = self.updates.setdefault(row_id, {})
        entry[column] = value
        if version is not None and VERSION_COLUMN not in entry:
            entry[VERSION_COLUMN] = int(version)

    def add_row(self, values: Mapping[str, Any] | None = None) -> str:
        row_id = new_temp_id()
        self.inserts[row_id] = dict(values or {})
        return row_id

    def mark_deleted(self, row_id: str, version: Any = None, label: str = "") -> None:
        if is_temp_id(row_id):
            self.inserts.pop(row_id, None)
            return
        self.updates.pop(row_id, None)
        self.deletes[row_id] = {VERSION_COLUMN: None if version is None else int(version), "label": label}

    def set_many(self, rows: Iterable[Mapping[str, Any]], column: str, value: Any) -> list[str]:
        """Bulk update: set ``column`` to ``value`` on every row (existing or pending); returns the ids."""
        touched = []
        for row in rows:
            rid = str(row.get(ID_COLUMN) or "")
            if not rid:
                continue
            self.set_cell(rid, column, value, row.get(VERSION_COLUMN))
            touched.append(rid)
        return touched

    def clear(self) -> None:
        self.updates.clear()
        self.inserts.clear()
        self.deletes.clear()

    # -- overlay ---------------------------------------------------------------------------

    def edits(self, row_id: str) -> dict[str, Any]:
        """The pending cell values of an existing row (without the version token)."""
        return {k: v for k, v in self.updates.get(row_id, {}).items() if k != VERSION_COLUMN}

    def new_row(self, form: FormDef, tmp_id: str) -> dict[str, Any]:
        """A grid row for a pending insert: every column, the pending values, flagged as new."""
        row = {c.name: None for c in form.columns}
        row.update(self.inserts.get(tmp_id, {}))
        row[ID_COLUMN] = tmp_id
        row[VERSION_COLUMN] = None
        row[NEW_FLAG] = True
        return row

    def apply_to_rows(self, rows: Iterable[dict[str, Any]], form: FormDef) -> list[dict[str, Any]]:
        """Re-apply the draft on freshly loaded rows (after a search or refresh)."""
        out: list[dict[str, Any]] = [self.new_row(form, tmp_id) for tmp_id in self.inserts]
        for row in rows:
            rid = str(row.get(ID_COLUMN))
            if rid in self.deletes:
                continue
            if rid in self.updates:
                row = {**row, **self.edits(rid)}
            out.append(row)
        return out


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


def build_changeset_from_draft(
    form: FormDef, rows: Iterable[Mapping[str, Any]], draft: Draft
) -> tuple[ChangeSet, list[ValidationIssue]]:
    """Validate a draft against the loaded rows and produce a :class:`ChangeSet`."""
    changes = ChangeSet()
    issues: list[ValidationIssue] = []
    user_cols = {c.name: c for c in form.user_columns}
    # Rows the grid already shows for pending inserts carry temporary ids; the draft is their
    # source of truth, so keep only persisted rows here.
    by_id = {str(r.get(ID_COLUMN)): dict(r) for r in rows if not is_temp_id(r.get(ID_COLUMN))}

    def coerce_into(target: dict[str, Any], col: ColumnDef, raw: Any, label: str, rid: str) -> None:
        if col.data_type is DataType.OTHER:
            issues.append(ValidationIssue(label, col.name, "column is read-only", rid))
            return
        try:
            value = coerce_value(col, raw)
        except CoercionError as exc:
            issues.append(ValidationIssue(label, col.name, str(exc), rid))
            return
        if problem := rule_violation(col, value):
            issues.append(ValidationIssue(label, col.name, problem, rid))
        target[col.name] = value

    for rid, edits in draft.updates.items():
        base = by_id.get(rid)
        if base is None:
            continue  # row disappeared from the loaded set; the save will report it if stale
        merged = {**base, **draft.edits(rid)}
        label = describe_row(form, merged, fallback=f"row {rid[:8]}")
        values: dict[str, Any] = {}
        for col_name, raw in edits.items():
            col = user_cols.get(col_name)
            if col is None:
                continue
            coerce_into(values, col, raw, label, rid)
        if values:
            version = edits.get(VERSION_COLUMN, base.get(VERSION_COLUMN))
            version = None if is_missing(version) else int(version)
            changes.updates.append(RowUpdate(rid, values, version, label))

    for i, (tmp, raw_values) in enumerate(draft.inserts.items()):
        label = f"New row {i + 1}"
        values = {}
        for col in user_cols.values():
            if col.data_type is DataType.OTHER:
                continue
            coerce_into(values, col, raw_values.get(col.name), label, tmp)
        if all(v is None for v in values.values()):
            issues.append(ValidationIssue(label, None, "is empty", tmp))
        changes.inserts.append(RowInsert(values, label))

    for rid, info in draft.deletes.items():
        base = by_id.get(rid, {})
        label = info.get("label") or describe_row(form, base, fallback=f"row {rid[:8]}")
        version = info.get(VERSION_COLUMN, base.get(VERSION_COLUMN))
        version = None if is_missing(version) else int(version)
        changes.deletes.append(RowDelete(rid, version, label))

    snapshot = (
        pd.DataFrame(list(by_id.values())) if by_id else pd.DataFrame(columns=[c.name for c in form.columns])
    )
    label_to_id = {u.label: u.row_id for u in changes.updates}
    label_to_id.update({ins.label: tmp for ins, tmp in zip(changes.inserts, draft.inserts, strict=False)})
    for issue in check_unique_keys(form, snapshot, changes):
        issue.row_id = label_to_id.get(issue.row_label)
        issues.append(issue)
    return changes, issues


def check_unique_keys(form: FormDef, snapshot: pd.DataFrame, changes: ChangeSet) -> list[ValidationIssue]:
    """Business-key uniqueness across the loaded rows after applying the change set.

    Rows that are not touched by the change set are never reported (they may already be
    duplicates in the table); every edited or new row that duplicates any other row is,
    whatever the order of the loaded rows.
    """
    keys = [c.name for c in form.key_columns]
    if not keys or (snapshot.empty and not changes.inserts):
        return []
    deleted = {d.row_id for d in changes.deletes}
    updates = {u.row_id: u.changes for u in changes.updates}

    def key_of(values: Mapping[str, Any]) -> tuple:
        return tuple(_norm_key(values.get(k)) for k in keys)

    kept: dict[tuple, str] = {}
    edited: list[tuple[tuple, str]] = []
    if not snapshot.empty and ID_COLUMN in snapshot.columns:
        for rec in snapshot[[ID_COLUMN, *[k for k in keys if k in snapshot.columns]]].to_dict("records"):
            rid = str(rec[ID_COLUMN])
            if rid in deleted:
                continue
            merged = {**rec, **updates.get(rid, {})}
            key = key_of(merged)
            if all(k is None for k in key):
                continue
            if rid in updates:
                edited.append((key, describe_row(form, merged)))
            else:
                kept.setdefault(key, describe_row(form, rec))
    issues: list[ValidationIssue] = []
    seen = dict(kept)
    for key, label in edited:
        if key in seen:
            issues.append(ValidationIssue(label, ", ".join(keys), f"duplicates existing {seen[key]}"))
        else:
            seen[key] = label
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


def invalid_cells(issues: Iterable[ValidationIssue]) -> dict[str, list[str]]:
    """``{row_id: [column, ...]}`` for issues tied to a row (used to highlight grid cells)."""
    out: dict[str, list[str]] = {}
    for i in issues:
        if i.row_id and i.column:
            cols = out.setdefault(i.row_id, [])
            if i.column not in cols:
                cols.append(i.column)
    return out


# --------------------------------------------------------------------------------------
# Bulk update and restore helpers (item form / row history)
# --------------------------------------------------------------------------------------


def json_safe(value: Any) -> Any:
    """The representation the browser grid uses for a coerced value (what the draft stores)."""
    if value is None:
        return None
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "item"):
        return value.item()
    return str(value)


def bulk_value(form: FormDef, column: str, raw: Any) -> Any:
    """Validate one value for a bulk update of ``column``; returns the value to store in the draft.

    Raises :class:`ValueError` with a user-facing message when the value cannot be applied.
    """
    col = form.column(column)
    if col is None or col.is_system:
        raise ValueError(f"Unknown column '{column}'.")
    if col.data_type is DataType.OTHER:
        raise ValueError(f"Column '{column}' is read-only.")
    try:
        value = coerce_value(col, raw)
    except CoercionError as exc:
        raise ValueError(f"'{column}': {exc}") from exc
    if value is None and col.required:
        raise ValueError(f"'{column}' is required and cannot be cleared.")
    if problem := rule_violation(col, value):
        raise ValueError(f"'{column}' {problem}")
    return json_safe(value)


def same_value(col: ColumnDef, a: Any, b: Any) -> bool:
    """Whether two raw representations (grid JSON vs. history JSON) denote the same value."""
    try:
        return coerce_value(col, a) == coerce_value(col, b)
    except CoercionError:
        return ("" if is_missing(a) else str(a)) == ("" if is_missing(b) else str(b))


def restore_row(
    draft: Draft, form: FormDef, current: Mapping[str, Any] | None, snapshot: Mapping[str, Any]
) -> tuple[str, int]:
    """Stage the values of a historical version of a row into the draft.

    ``current`` is the row as shown in the grid (``None`` when the row no longer exists, e.g.
    restoring a deleted row), ``snapshot`` the values recorded in the history. Returns the id
    of the row the values were staged on (a temporary id for a re-created row) and the number
    of columns that changed. Nothing is written until the user saves.
    """
    columns = [c for c in form.user_columns if c.data_type is not DataType.OTHER]
    values: dict[str, Any] = {}
    for c in columns:
        raw = snapshot.get(c.name)
        try:
            values[c.name] = json_safe(coerce_value(c, raw))
        except CoercionError:
            values[c.name] = None if is_missing(raw) else raw
    if current is None or is_missing(current.get(ID_COLUMN)):
        return draft.add_row(values), len(values)
    rid = str(current[ID_COLUMN])
    changed = 0
    for c in columns:
        if not same_value(c, current.get(c.name), values[c.name]):
            draft.set_cell(rid, c.name, values[c.name], current.get(VERSION_COLUMN))
            changed += 1
    return rid, changed
