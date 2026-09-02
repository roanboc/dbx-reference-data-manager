"""Build the backend and auth provider selected by :class:`rdm.config.Settings`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from rdm.auth.provider import AuthProvider, DatabricksAuthProvider, MockAuthProvider
from rdm.backend.base import DatabaseBackend
from rdm.config import Settings


def create_backend(settings: Settings, access_token: str | None = None) -> DatabaseBackend:
    """Return a backend for ``settings``.

    ``access_token`` is the signed-in user's token when Databricks user authorization
    (on-behalf-of-user) is enabled; the Databricks backend then runs SQL as that user.
    """
    if settings.backend == "duckdb":
        from rdm.backend.duckdb_backend import DuckDBBackend

        return DuckDBBackend(settings.duckdb_path)
    if settings.backend == "databricks":
        from rdm.backend.databricks_backend import DatabricksBackend

        if not settings.warehouse_http_path:
            raise RuntimeError(
                "DATABRICKS_WAREHOUSE_ID (or DATABRICKS_HTTP_PATH) must be set for the Databricks backend."
            )
        return DatabricksBackend(
            catalog=settings.catalog,
            http_path=settings.warehouse_http_path,
            host=settings.databricks_host,
            access_token=access_token,
        )
    raise RuntimeError(f"Unknown backend '{settings.backend}' (expected 'duckdb' or 'databricks').")


def create_auth_provider(
    settings: Settings, headers_getter: Callable[[], Mapping[str, Any]] | None = None
) -> AuthProvider:
    if settings.auth == "mock":
        return MockAuthProvider(default_persona=settings.persona)
    if settings.auth == "databricks":
        if headers_getter is None:
            import streamlit as st

            def headers_getter() -> Mapping[str, Any]:  # type: ignore[no-redef]
                return st.context.headers

        return DatabricksAuthProvider(headers_getter)
    raise RuntimeError(f"Unknown auth provider '{settings.auth}' (expected 'mock' or 'databricks').")
