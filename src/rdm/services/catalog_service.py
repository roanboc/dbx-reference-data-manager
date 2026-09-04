"""Navigation model: which functions, forms and files the current user may see, grouped by domain."""

from __future__ import annotations

from dataclasses import dataclass, field

from rdm.backend.base import DatabaseBackend, PermissionDenied
from rdm.models import (
    UNASSIGNED_DOMAIN_LABEL,
    DomainDef,
    FileDef,
    FormDef,
    FunctionDef,
    Permissions,
    Role,
    User,
)


@dataclass
class NavFunction:
    function: FunctionDef
    role: Role
    forms: list[FormDef] = field(default_factory=list)
    files: list[FileDef] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.forms and not self.files


@dataclass
class NavDomain:
    """A domain with the visible functions assigned to it (``domain.name == ""`` = unassigned)."""

    domain: DomainDef
    functions: list[NavFunction] = field(default_factory=list)

    @property
    def is_unassigned(self) -> bool:
        return not self.domain.name


def _matches(text: str, *fields: str) -> bool:
    needle = text.strip().lower()
    if not needle:
        return True
    return any(needle in (f or "").lower() for f in fields)


class CatalogService:
    def __init__(self, backend: DatabaseBackend, user: User, permissions: Permissions) -> None:
        self.backend = backend
        self.user = user
        self.permissions = permissions

    def visible_functions(self) -> list[NavFunction]:
        out = []
        for f in self.backend.list_functions():
            role = self.permissions.role_for(f.name)
            if role.can_view:
                out.append(NavFunction(f, role))
        return out

    def navigation(self, search: str | None = None) -> list[NavFunction]:
        """Visible functions with their forms and files, filtered by ``search`` on names and descriptions."""
        return self.filter(self.load_navigation(), search)

    def load_navigation(self) -> list[NavFunction]:
        """The unfiltered catalogue the user may see; the expensive part, worth caching."""
        items = self.visible_functions()
        for item in items:
            item.forms = self.backend.list_forms(item.function.name)
            item.files = self.backend.list_files(item.function.name)
        return items

    def filter(self, items: list[NavFunction], search: str | None = None) -> list[NavFunction]:
        """Filter loaded navigation in memory - no backend calls."""
        if not search or not search.strip():
            return items
        filtered = []
        for item in items:
            f = item.function
            forms = [x for x in item.forms if _matches(search, x.name, x.title, x.description, x.owner)]
            files = [x for x in item.files if _matches(search, x.name, x.title, x.description, x.owner)]
            if forms or files:
                # Matching forms and files take precedence: show only them under their function.
                filtered.append(NavFunction(f, item.role, forms, files))
            elif _matches(search, f.name, f.title, f.description, f.owner, f.domain):
                filtered.append(item)
        return filtered

    def get_file(self, function: str, name: str) -> FileDef:
        if not self.permissions.role_for(function).can_view:
            raise PermissionDenied(f"You do not have access to function '{function}'.")
        return self.backend.get_file(function, name)

    def grouped(
        self, items: list[NavFunction], domains: list[DomainDef] | None = None
    ) -> list[NavDomain]:
        """Group navigation items by domain, in domain order; unassigned functions come last."""
        known = {d.name: d for d in (self.backend.list_domains() if domains is None else domains)}
        groups: dict[str, NavDomain] = {}
        for item in items:
            key = item.function.domain if item.function.domain in known else ""
            if key not in groups:
                domain = known.get(key) or DomainDef("", display_name=UNASSIGNED_DOMAIN_LABEL)
                groups[key] = NavDomain(domain)
            groups[key].functions.append(item)
        ordered = [groups[name] for name in known if name in groups]
        if "" in groups:
            ordered.append(groups[""])
        return ordered

    def get_form(self, function: str, name: str) -> FormDef:
        if not self.permissions.role_for(function).can_view:
            raise PermissionDenied(f"You do not have access to function '{function}'.")
        return self.backend.get_form(function, name)
