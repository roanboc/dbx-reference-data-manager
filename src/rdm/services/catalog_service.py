"""Navigation model: which domains and forms the current user may see, with search."""

from __future__ import annotations

from dataclasses import dataclass, field

from rdm.backend.base import DatabaseBackend, PermissionDenied
from rdm.models import DomainDef, FormDef, Permissions, Role, User


@dataclass
class NavDomain:
    domain: DomainDef
    role: Role
    forms: list[FormDef] = field(default_factory=list)


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

    def visible_domains(self) -> list[NavDomain]:
        out = []
        for d in self.backend.list_domains():
            role = self.permissions.role_for(d.name)
            if role.can_view:
                out.append(NavDomain(d, role))
        return out

    def navigation(self, search: str | None = None) -> list[NavDomain]:
        """Visible domains with their forms, filtered by ``search`` on names and descriptions."""
        items = self.visible_domains()
        for item in items:
            item.forms = self.backend.list_forms(item.domain.name)
        if not search or not search.strip():
            return items
        filtered = []
        for item in items:
            d = item.domain
            if _matches(search, d.name, d.title, d.description):
                filtered.append(item)
                continue
            forms = [f for f in item.forms if _matches(search, f.name, f.title, f.description, f.owner)]
            if forms:
                filtered.append(NavDomain(d, item.role, forms))
        return filtered

    def get_form(self, domain: str, name: str) -> FormDef:
        if not self.permissions.role_for(domain).can_view:
            raise PermissionDenied(f"You do not have access to domain '{domain}'.")
        return self.backend.get_form(domain, name)
