"""Per-request application context for Dash callbacks.

Dash callbacks are plain Flask requests: identity comes from the request headers (or the
local persona store), the backend is looked up from a small process-level cache (one
shared connection for DuckDB / service-principal mode, one per user token under
on-behalf-of-user authorization) and permissions are cached briefly per user.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from functools import lru_cache

import flask

from rdm.auth.provider import PERSONAS, AuthProvider
from rdm.backend.base import DatabaseBackend
from rdm.backend.factory import create_auth_provider, create_backend
from rdm.config import Settings
from rdm.models import DomainDef, Permissions, Role, User
from rdm.services import CatalogService, FormService, NavDomain, NavFunction

log = logging.getLogger(__name__)

_lock = threading.RLock()
_backends: dict[str, tuple[float, DatabaseBackend]] = {}
_permissions: dict[tuple, tuple[float, Permissions]] = {}
_navigation: dict[tuple, tuple[float, list[NavFunction]]] = {}
_domains: dict[tuple, tuple[float, list[DomainDef]]] = {}
TOKEN_BACKEND_TTL = 900.0


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_auth_provider() -> AuthProvider:
    return create_auth_provider(get_settings(), headers_getter=lambda: flask.request.headers)


def get_backend(settings: Settings, access_token: str | None) -> DatabaseBackend:
    if access_token and settings.is_databricks:
        key = "token:" + hashlib.sha256(access_token.encode()).hexdigest()[:24]
        ttl = TOKEN_BACKEND_TTL
    else:
        key = "shared"
        ttl = float("inf")
    now = time.monotonic()
    with _lock:
        cached = _backends.get(key)
        if cached and cached[0] > now:
            return cached[1]
        if cached:
            try:
                cached[1].close()
            except Exception:  # noqa: BLE001
                pass
        backend = create_backend(settings, access_token=access_token)
        _backends[key] = (now + ttl, backend)
        # drop expired per-user connections
        for k in [k for k, (exp, _) in _backends.items() if exp <= now]:
            _backends.pop(k, None)
        return backend


@dataclass
class AppContext:
    settings: Settings
    auth: AuthProvider
    backend: DatabaseBackend
    user: User
    permissions: Permissions
    catalog: CatalogService
    forms: FormService

    @property
    def role_of(self):
        return self.permissions.role_for


def get_context(persona: str | None = None) -> AppContext:
    settings = get_settings()
    auth = get_auth_provider()
    user = auth.current_user(persona if auth.supports_persona_switching else None)
    backend = get_backend(settings, auth.access_token())
    permissions = _permissions_for(settings, backend, user)
    return AppContext(
        settings=settings,
        auth=auth,
        backend=backend,
        user=user,
        permissions=permissions,
        catalog=CatalogService(backend, user, permissions),
        forms=FormService(backend, user, permissions),
    )


def _permissions_for(settings: Settings, backend: DatabaseBackend, user: User) -> Permissions:
    ttl = settings.metadata_cache_ttl
    key = (user.username, tuple(user.groups), id(backend))
    now = time.monotonic()
    if ttl > 0:
        with _lock:
            cached = _permissions.get(key)
            if cached and cached[0] > now:
                return cached[1]
    perms = (
        _simulated_permissions(backend, user) if settings.debug_personas else backend.get_permissions(user)
    )
    if ttl > 0:
        with _lock:
            _permissions[key] = (now + ttl, perms)
    return perms


#: FR-45: role each persona experiences on every function when RDM_DEBUG_PERSONAS is on.
_SIMULATED_ROLES: dict[str, tuple[bool, Role]] = {
    "admin": (True, Role.ADMIN),
    "function_admin": (False, Role.ADMIN),
    "editor": (False, Role.EDITOR),
    "viewer": (False, Role.VIEWER),
}


def _simulated_permissions(backend: DatabaseBackend, user: User) -> Permissions:
    """Simulated access for review deployments: the persona's role applies to every function.

    Queries still run as the app's own principal, so this mode belongs only on a dev
    deployment with test data (set ``RDM_DEBUG_PERSONAS`` there and nowhere else).
    """
    key = next((p.key for p in PERSONAS.values() if p.user.username == user.username), "viewer")
    is_global, role = _SIMULATED_ROLES[key]
    return Permissions({f.name: role for f in backend.list_functions()}, is_global_admin=is_global)


def navigation(ctx: AppContext, search: str | None) -> list[NavFunction]:
    """Visible functions (with their forms) for the sidebar and the home page, cached briefly.

    The cache holds the unfiltered catalogue: filtering happens in memory, so typing in the
    sidebar or home filter costs no warehouse round trips.
    """
    return ctx.catalog.filter(_loaded_navigation(ctx), search)


def _loaded_navigation(ctx: AppContext) -> list[NavFunction]:
    ttl = ctx.settings.metadata_cache_ttl
    key = (ctx.user.username, tuple(ctx.user.groups), id(ctx.backend))
    now = time.monotonic()
    if ttl > 0:
        with _lock:
            cached = _navigation.get(key)
            if cached and cached[0] > now:
                return cached[1]
    items = ctx.catalog.load_navigation()
    domains = ctx.backend.list_domains()
    if ttl > 0:
        with _lock:
            _navigation[key] = (now + ttl, items)
            _domains[key] = (now + ttl, domains)
    return items


def grouped_navigation(ctx: AppContext, search: str | None) -> list[NavDomain]:
    """Navigation items grouped by domain (domain > function > form)."""
    items = navigation(ctx, search)
    key = (ctx.user.username, tuple(ctx.user.groups), id(ctx.backend))
    now = time.monotonic()
    with _lock:
        cached = _domains.get(key)
    domains = cached[1] if cached and cached[0] > now else None
    return ctx.catalog.grouped(items, domains)


def invalidate_metadata() -> None:
    """Forget cached permissions and navigation (after administrative changes)."""
    with _lock:
        _permissions.clear()
        _navigation.clear()
        _domains.clear()
