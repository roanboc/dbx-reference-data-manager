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

from rdm.auth.provider import AuthProvider
from rdm.backend.base import DatabaseBackend
from rdm.backend.factory import create_auth_provider, create_backend
from rdm.config import Settings
from rdm.models import Permissions, User
from rdm.services import CatalogService, FormService, NavDomain

log = logging.getLogger(__name__)

_lock = threading.RLock()
_backends: dict[str, tuple[float, DatabaseBackend]] = {}
_permissions: dict[tuple, tuple[float, Permissions]] = {}
_navigation: dict[tuple, tuple[float, list[NavDomain]]] = {}
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
    perms = backend.get_permissions(user)
    if ttl > 0:
        with _lock:
            _permissions[key] = (now + ttl, perms)
    return perms


def navigation(ctx: AppContext, search: str | None) -> list[NavDomain]:
    ttl = ctx.settings.metadata_cache_ttl
    key = (ctx.user.username, tuple(ctx.user.groups), (search or "").strip().lower(), id(ctx.backend))
    now = time.monotonic()
    if ttl > 0:
        with _lock:
            cached = _navigation.get(key)
            if cached and cached[0] > now:
                return cached[1]
    items = ctx.catalog.navigation(search)
    if ttl > 0:
        with _lock:
            _navigation[key] = (now + ttl, items)
    return items


def invalidate_metadata() -> None:
    """Forget cached permissions and navigation (after administrative changes)."""
    with _lock:
        _permissions.clear()
        _navigation.clear()
