"""Session/resource wiring for the Streamlit app.

* Resources (settings, backend, auth provider) are created once per process with
  ``st.cache_resource``; the Databricks backend is cached per user token.
* Navigation metadata is cached briefly (``RDM_METADATA_CACHE_TTL``) and cleared after
  any administrative change.
* The current *view* (home / form / domain / new-form / new-domain) lives in
  ``st.session_state`` and is mirrored to the URL query string for deep links.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

import streamlit as st

from rdm.auth.provider import AuthProvider
from rdm.backend.base import DatabaseBackend
from rdm.backend.factory import create_auth_provider, create_backend
from rdm.config import Settings
from rdm.models import Permissions, User
from rdm.services import CatalogService, FormService, NavDomain

log = logging.getLogger(__name__)

VIEW_HOME = "home"
VIEW_FORM = "form"
VIEW_DOMAIN = "domain"
VIEW_NEW_FORM = "new-form"
VIEW_NEW_DOMAIN = "new-domain"

_VALID_VIEWS = {VIEW_HOME, VIEW_FORM, VIEW_DOMAIN, VIEW_NEW_FORM, VIEW_NEW_DOMAIN}


# --------------------------------------------------------------------------------------
# Resources
# --------------------------------------------------------------------------------------


@st.cache_resource(show_spinner=False)
def get_settings() -> Settings:
    return Settings.from_env()


@st.cache_resource(show_spinner=False)
def get_auth_provider() -> AuthProvider:
    return create_auth_provider(get_settings())


@st.cache_resource(show_spinner=False)
def _shared_backend(kind: str, target: str) -> DatabaseBackend:
    """One backend per process for connections that are not user specific (DuckDB, SP mode)."""
    return create_backend(get_settings())


@st.cache_resource(ttl=900, show_spinner=False)
def _user_backend(kind: str, target: str, token_hash: str, token: str) -> DatabaseBackend:
    """One backend per signed-in user when on-behalf-of-user authorization is enabled."""
    return create_backend(get_settings(), access_token=token)


def get_backend(settings: Settings, access_token: str | None) -> DatabaseBackend:
    target = settings.duckdb_path if settings.backend == "duckdb" else (settings.warehouse_http_path or "")
    if access_token and settings.is_databricks:
        token_hash = hashlib.sha256(access_token.encode()).hexdigest()[:16]
        return _user_backend(settings.backend, target, token_hash, access_token)
    return _shared_backend(settings.backend, target)


# --------------------------------------------------------------------------------------
# Context
# --------------------------------------------------------------------------------------


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
    def persona_key(self) -> str | None:
        return st.session_state.get("persona") if self.auth.supports_persona_switching else None


def get_context() -> AppContext:
    settings = get_settings()
    auth = get_auth_provider()
    persona = None
    if auth.supports_persona_switching:
        persona = st.session_state.setdefault("persona", settings.persona)
    user = auth.current_user(persona)
    backend = get_backend(settings, auth.access_token())
    permissions = _permissions_for(backend, user)
    return AppContext(
        settings=settings,
        auth=auth,
        backend=backend,
        user=user,
        permissions=permissions,
        catalog=CatalogService(backend, user, permissions),
        forms=FormService(backend, user, permissions),
    )


def _permissions_for(backend: DatabaseBackend, user: User) -> Permissions:
    """Permissions are cached per user per session; ``invalidate_metadata`` clears them."""
    cache = st.session_state.setdefault("_permissions", {})
    key = (user.username, tuple(user.groups))
    if key not in cache:
        cache[key] = backend.get_permissions(user)
    return cache[key]


# --------------------------------------------------------------------------------------
# Navigation metadata cache
# --------------------------------------------------------------------------------------


def navigation(ctx: AppContext, search: str | None) -> list[NavDomain]:
    ttl = ctx.settings.metadata_cache_ttl
    if ttl <= 0:
        return ctx.catalog.navigation(search)
    return _navigation_cached(ctx.user.username, tuple(ctx.user.groups), search or "", ttl)


@st.cache_data(ttl=60, show_spinner=False)
def _navigation_cached(username: str, groups: tuple[str, ...], search: str, ttl: int) -> list[NavDomain]:
    ctx = get_context()
    return ctx.catalog.navigation(search or None)


def invalidate_metadata() -> None:
    """Call after creating/altering domains or forms so navigation and permissions refresh."""
    _navigation_cached.clear()
    st.session_state.pop("_permissions", None)
    for key in [k for k in st.session_state if str(k).startswith("form-def::")]:
        st.session_state.pop(key, None)


# --------------------------------------------------------------------------------------
# View state
# --------------------------------------------------------------------------------------


@dataclass
class ViewState:
    view: str = VIEW_HOME
    domain: str | None = None
    form: str | None = None


def current_view() -> ViewState:
    if "view" not in st.session_state:
        qp = st.query_params
        view = qp.get("view") or (
            VIEW_FORM if qp.get("form") else VIEW_DOMAIN if qp.get("domain") else VIEW_HOME
        )
        st.session_state["view"] = ViewState(
            view if view in _VALID_VIEWS else VIEW_HOME, qp.get("domain") or None, qp.get("form") or None
        )
    return st.session_state["view"]


def set_view(view: str, domain: str | None = None, form: str | None = None) -> None:
    st.session_state["view"] = ViewState(view, domain, form)
    params = {"view": view}
    if domain:
        params["domain"] = domain
    if form:
        params["form"] = form
    st.query_params.clear()
    st.query_params.update(params)


def go_home() -> None:
    set_view(VIEW_HOME)


def open_form(domain: str, form: str) -> None:
    set_view(VIEW_FORM, domain, form)


def open_domain(domain: str) -> None:
    set_view(VIEW_DOMAIN, domain)


# --------------------------------------------------------------------------------------
# Editor state helpers
# --------------------------------------------------------------------------------------


def editor_version(form_full_name: str) -> int:
    return int(st.session_state.get(f"editor-version::{form_full_name}", 0))


def bump_editor_version(form_full_name: str) -> None:
    key = f"editor-version::{form_full_name}"
    st.session_state[key] = editor_version(form_full_name) + 1
    for k in [k for k in st.session_state if str(k).startswith(f"snapshot::{form_full_name}::")]:
        st.session_state.pop(k, None)


def editor_key(form_full_name: str) -> str:
    return f"editor::{form_full_name}::{editor_version(form_full_name)}"


def has_pending_changes() -> bool:
    """True when the data editor of the form being viewed has unsaved edits."""
    key = st.session_state.get("active_editor_key")
    if not key:
        return False
    state = st.session_state.get(key) or {}
    return bool(state.get("edited_rows") or state.get("added_rows") or state.get("deleted_rows"))
