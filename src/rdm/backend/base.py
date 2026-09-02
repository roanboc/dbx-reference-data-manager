"""The repository interface every backend implements.

The Streamlit layer and the services only ever talk to :class:`DatabaseBackend`. No SQL
lives outside ``rdm.backend``. Backends receive the acting :class:`~rdm.models.User` for
audit stamping; authorisation decisions are made in the service layer (and, in
production, enforced again by Unity Catalog because statements run as the user).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from rdm.models import ChangeSet, ColumnDef, DomainDef, FormDef, Permissions, Role, SaveResult, User


class BackendError(Exception):
    """Base class for errors raised by a backend (safe to show to the user)."""


class NotFoundError(BackendError):
    """A domain, form or column does not exist."""


class PermissionDenied(BackendError):
    """The acting user is not allowed to perform the operation."""


class ConflictError(BackendError):
    """An object with that name already exists."""


class DatabaseBackend(ABC):
    """Abstract repository over a catalog of domains (schemas) and forms (tables)."""

    #: Short backend identifier shown in the UI ("duckdb", "databricks").
    name: str = "abstract"

    # -- lifecycle -------------------------------------------------------------------------

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release connections. Safe to call more than once."""

    def describe(self) -> str:
        """One-line human description of the connection target (shown in the sidebar)."""
        return self.name

    # -- domains -----------------------------------------------------------------------------

    @abstractmethod
    def list_domains(self) -> list[DomainDef]:
        """All domains in the catalog (regardless of the caller's access)."""

    @abstractmethod
    def get_domain(self, name: str) -> DomainDef:
        """Raise :class:`NotFoundError` if the domain does not exist."""

    @abstractmethod
    def create_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        """Create a schema with description/owner metadata. The actor becomes its administrator.

        Production backends may refuse this (domains managed as infrastructure-as-code).
        """

    @abstractmethod
    def update_domain(self, domain: DomainDef, actor: User) -> DomainDef:
        """Update description, display name and owner of an existing domain."""

    # -- forms -------------------------------------------------------------------------------

    @abstractmethod
    def list_forms(self, domain: str) -> list[FormDef]:
        """Lightweight form list for navigation (no columns, no row counts guaranteed)."""

    @abstractmethod
    def get_form(self, domain: str, name: str) -> FormDef:
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
        """Delete the table and its metadata."""

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
    def get_history(self, form: FormDef, limit: int = 200) -> pd.DataFrame:
        """Row-level change history, newest first. Columns: ``HISTORY_COLUMNS`` + row columns."""

    # -- authorisation -------------------------------------------------------------------

    @abstractmethod
    def get_permissions(self, user: User) -> Permissions:
        """Effective role of ``user`` for every domain, plus catalog-level capabilities."""

    @abstractmethod
    def list_domain_grants(self, domain: str) -> list[tuple[str, Role]]:
        """(principal, role) pairs granted on a domain."""

    @abstractmethod
    def grant_domain_role(self, domain: str, principal: str, role: Role, actor: User) -> None:
        """Grant (or, with ``Role.NONE``, revoke) a role on a domain to a *group*.

        Individual users are rejected: access is always managed through groups.
        """

    def list_groups(self, query: str | None = None) -> list[str]:
        """Groups that can be granted access (best effort; empty when unknown)."""
        return []
