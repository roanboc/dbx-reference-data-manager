"""Guarded persistence: every UI action on forms, files, functions and domains passes a role check here."""

from __future__ import annotations

import logging

import pandas as pd

from rdm.backend.base import DatabaseBackend, PermissionDenied
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

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------------------
# Guarded service facade
# --------------------------------------------------------------------------------------


class FormService:
    """Everything the UI does with forms, files, functions and domains, with role checks in front of the backend."""

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
        return self.backend.create_form(form, self.user, rows)

    def update_form_metadata(self, form: FormDef) -> FormDef:
        self.require(form.function, Role.ADMIN)
        return self.backend.update_form_metadata(form, self.user)

    def add_column(self, form: FormDef, column: ColumnDef) -> FormDef:
        self.require(form.function, Role.ADMIN)
        return self.backend.add_column(form, column, self.user)

    def drop_column(self, form: FormDef, column_name: str) -> FormDef:
        self.require(form.function, Role.ADMIN)
        return self.backend.drop_column(form, column_name, self.user)

    def drop_form(self, form: FormDef) -> None:
        """Deleting a form (dropping its table) is reserved to global admins."""
        self.require_global_admin("Deleting a form")
        log.info("%s dropping form %s", self.user.username, form.full_name)
        self.backend.drop_form(form, self.user)

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
        log.info("%s adding file %s (%d bytes)", self.user.username, file.full_name, len(data))
        return self.backend.put_file(file, data, self.user, replace=False)

    def replace_file(self, file: FileDef, data: bytes) -> FileDef:
        """Replacing the content of a file is an editor act, like changing rows."""
        self.require(file.function, Role.EDITOR)
        log.info("%s replacing file %s (%d bytes)", self.user.username, file.full_name, len(data))
        return self.backend.put_file(file, data, self.user, replace=True)

    def update_file_metadata(self, file: FileDef) -> FileDef:
        self.require(file.function, Role.ADMIN)
        return self.backend.update_file_metadata(file, self.user)

    def drop_file(self, file: FileDef) -> None:
        """Deleting a file is reserved to global admins, like deleting a form."""
        self.require_global_admin("Deleting a file")
        log.info("%s dropping file %s", self.user.username, file.full_name)
        self.backend.drop_file(file, self.user)

    # -- functions (schemas) ---------------------------------------------------------------

    def create_function(self, function: FunctionDef) -> FunctionDef:
        self.require_global_admin("Creating a function")
        return self.backend.create_function(function, self.user)

    def update_function(self, function: FunctionDef) -> FunctionDef:
        """Function admins edit the details; moving a function to another domain is a global-admin act."""
        self.require(function.name, Role.ADMIN)
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
