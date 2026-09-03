"""The factory turns Settings into the backend and auth provider the app runs with."""

from __future__ import annotations

import pytest

from rdm.auth.provider import DatabricksAuthProvider, MockAuthProvider
from rdm.backend.databricks_backend import DatabricksBackend
from rdm.backend.duckdb_backend import DuckDBBackend
from rdm.backend.factory import create_auth_provider, create_backend
from rdm.config import Settings


def test_duckdb_backend_from_settings():
    backend = create_backend(Settings(backend="duckdb", duckdb_path=":memory:"))
    assert isinstance(backend, DuckDBBackend)
    backend.close()


def test_databricks_backend_needs_a_warehouse():
    settings = Settings(backend="databricks", catalog="_reference_data", databricks_warehouse_id="w1")
    backend = create_backend(
        settings, access_token="tok"
    )  # no connection is opened until the first statement
    assert isinstance(backend, DatabricksBackend)
    assert backend.http_path == "/sql/1.0/warehouses/w1" and backend.access_token == "tok"
    with pytest.raises(
        RuntimeError, match="DATABRICKS_WAREHOUSE_ID \\(or DATABRICKS_HTTP_PATH\\) must be set"
    ):
        create_backend(Settings(backend="databricks"))
    with pytest.raises(RuntimeError, match="Unknown backend"):
        create_backend(Settings(backend="sqlite"))


def test_auth_provider_from_settings():
    mock = create_auth_provider(Settings(auth="mock", persona="viewer"))
    assert isinstance(mock, MockAuthProvider) and mock.default_persona == "viewer"
    dbx = create_auth_provider(Settings(auth="databricks"), headers_getter=lambda: {})
    assert isinstance(dbx, DatabricksAuthProvider)
    with pytest.raises(RuntimeError, match="Unknown auth provider"):
        create_auth_provider(Settings(auth="ldap"))
