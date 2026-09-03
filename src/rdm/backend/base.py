"""The repository interface every backend implements.

The Dash layer and the services only ever talk to :class:`DatabaseBackend`. No SQL lives
outside ``rdm.backend``. Backends receive the acting :class:`~rdm.models.User` for audit
stamping; authorisation decisions are made in the service layer (and, in production,
enforced again by Unity Catalog because statements run as the user).

Hierarchy: **domain** (a classifier kept in the registry, maintained by global admins) >
**function** (a schema) > **form** (a table) or **file** (a CSV/Parquet file in the
function's volume).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from rdm.models import (
    ChangeSet,
    ColumnDef,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    SaveResult,
    User,
)


class BackendError(Exception):
    """Base class for errors raised by a backend (safe to show to the user)."""


class NotFoundError(BackendError):
    """A domain, function, form or column does not exist."""


class PermissionDenied(BackendError):
    """The acting user is not allowed to perform the operation."""


class ConflictError(BackendError):
    """An object with that name already exists, or an object is still in use."""


class DatabaseBackend(ABC):
    """Abstract repository over a catalog of functions (schemas) and forms (tables)."""

    #: Short backend identifier ("duckdb", "databricks"), shown as a badge in the header.
    name: str = "abstract"

    # -- lifecycle -------------------------------------------------------------------------

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections. Safe to call more than once."""

    def describe(self) -> str:
        """One-line human description of the connection target (tooltip of the header badge)."""
        return self.name

    # -- domains (classifier above functions) -------------------------------------------

    @abstractmethod
    def list_domains(self) -> list[DomainDef]:
        """Every domain in the registry, with the number of functions assigned to it."""

    @abstractmethod
    def get_domain(self, name: str) -> DomainDef:
        """Raise :class:`NotFoundError` if the domain does not exist."""

    @abstractmethod
    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        """Add a domain to the registry (global admins only, enforced by the service)."""

    @abstractmethod
    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        """Update display name, description and owner of a domain."""

    @abstractmethod
    def delete_domain(self, name: str, actor: User) -> None:
        """Remove a domain. Raises :class:`ConflictError` while functions are still assigned."""

    # -- functions (schemas) -------------------------------------------------------------

    @abstractmethod
    def list_functions(self) -> list[FunctionDef]:
        """Every function (schema) with its counts; ``file_count`` counts the registered files."""

    @abstractmethod
    def get_function(self, name: str) -> FunctionDef:
        """Raise :class:`NotFoundError` if the function does not exist."""

    @abstractmethod
    def create_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        """Create a schema with description/owner/domain metadata (global admins only)."""

    @abstractmethod
    def update_function(self, function: FunctionDef, actor: User) -> FunctionDef:
        """Update description, display name, owner, documentation link and domain of a function."""

    @abstractmethod
    def drop_function(self, function: FunctionDef, actor: User) -> None:
        """Drop the schema and its metadata. Raises :class:`ConflictError` while it has forms or files."""

    # -- forms -------------------------------------------------------------------------------

    @abstractmethod
    def list_forms(self, function: str) -> list[FormDef]:
        """Lightweight form list for navigation (no columns, no row counts guaranteed)."""

    @abstractmethod
    def get_form(self, function: str, name: str) -> FormDef:
        """Full definition with columns, properties, tags and row count."""

    @abstractmethod
    def create_form(self, form: FormDef, actor: User, rows: pd.DataFrame | None = None) -> FormDef:
        """Create the table (system columns are added automatically) and optionally load rows."""

    @abstractmethod
    def update_form_metadata(self, form: FormDef, actor: User) -> FormDef:
        """Persist description, display name, owner, column descriptions, required flags and column config."""

    @abstractmethod
    def add_column(self, form: FormDef, column: ColumnDef, actor: User) -> FormDef:
        """Add a nullable column (with description and config) to an existing form."""

    @abstractmethod
    def drop_column(self, form: FormDef, column_name: str, actor: User) -> FormDef:
        """Remove a user column. System columns cannot be dropped."""

    @abstractmethod
    def drop_form(self, form: FormDef, actor: User) -> None:
        """Drop the table and its metadata; the audit entries are kept (like ``drop_file``)."""

    # -- rows --------------------------------------------------------------------------------

    @abstractmethod
    def read_rows(
        self,
        form: FormDef,
        search: str | None = None,
        limit: int = 5000,
        order_by: str | None = None,
        descending: bool = False,
    ) -> pd.DataFrame:
        """Rows including system columns, normalised dtypes, stable order.

        ``search`` matches any user column (case-insensitive substring); ``order_by`` is a
        column name (default: creation order).
        """

    @abstractmethod
    def count_rows(self, form: FormDef) -> int:
        """Total number of rows in the form."""

    @abstractmethod
    def apply_changes(self, form: FormDef, changes: ChangeSet, actor: User) -> SaveResult:
        """Apply inserts, updates and deletes. Rows changed concurrently are reported as conflicts."""

    @abstractmethod
    def append_rows(self, form: FormDef, rows: pd.DataFrame, actor: User) -> int:
        """Bulk insert rows (user columns only); returns the number of rows inserted."""

    @abstractmethod
    def get_history(self, form: FormDef, limit: int = 200, row_id: str | None = None) -> pd.DataFrame:
        """Row-level change history, newest first. Columns: ``HISTORY_COLUMNS`` + row columns.

        ``row_id`` restricts the history to one row (the item form shows it with a restore
        action per version).
        """

    # -- files (CSV / Parquet datasets in the function's volume) ---------------------------

    @abstractmethod
    def list_files(self, function: str) -> list[FileDef]:
        """Files of a function: registry entries merged with what the storage holds."""

    @abstractmethod
    def get_file(self, function: str, name: str) -> FileDef:
        """Raise :class:`NotFoundError` if the file does not exist in storage."""

    @abstractmethod
    def put_file(self, file: FileDef, data: bytes, actor: User, replace: bool = False) -> FileDef:
        """Store a new file (``replace=False``, conflict if it exists) or replace its content.

        Records size and row count, writes the registry entry and an audit entry.
        """

    @abstractmethod
    def update_file_metadata(self, file: FileDef, actor: User) -> FileDef:
        """Persist display name, description and owner of a file."""

    @abstractmethod
    def read_file(self, file: FileDef) -> bytes:
        """The file content (for download)."""

    @abstractmethod
    def preview_file(self, file: FileDef, limit: int = 100) -> pd.DataFrame:
        """The first ``limit`` rows of the file, parsed with the backend's reader."""

    @abstractmethod
    def file_columns(self, file: FileDef) -> list[ColumnDef]:
        """Column names and (native) types as the backend's reader infers them."""

    @abstractmethod
    def file_history(self, file: FileDef, limit: int = 200) -> pd.DataFrame:
        """Uploads, replacements and deletions of the file, newest first.

        Columns: ``version, changed_at, changed_by, change_type, size_bytes, row_count``.
        """

    @abstractmethod
    def drop_file(self, file: FileDef, actor: User) -> None:
        """Delete the file from storage and the registry (the audit entries are kept)."""

    # -- authorisation -------------------------------------------------------------------

    @abstractmethod
    def get_permissions(self, user: User) -> Permissions:
        """Effective role of ``user`` for every function, plus catalog-level capabilities."""

    @abstractmethod
    def list_function_grants(self, function: str) -> list[tuple[str, Role]]:
        """(principal, role) pairs granted on a function."""

    @abstractmethod
    def grant_function_role(self, function: str, principal: str, role: Role, actor: User) -> None:
        """Grant (or, with ``Role.NONE``, revoke) a role on a function to a *group*.

        Individual users are rejected: access is always managed through groups.
        """

    def list_groups(self, query: str | None = None) -> list[str]:
        """Groups that can be granted access (best effort; empty when unknown)."""
        return []
