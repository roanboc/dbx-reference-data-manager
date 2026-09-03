"""Tests for the auth providers and the environment-driven settings."""

from __future__ import annotations

import pytest

from rdm.auth import PERSONAS, AuthProvider, DatabricksAuthProvider, MockAuthProvider, Persona
from rdm.auth.provider import HEADER_ACCESS_TOKEN, HEADER_EMAIL, HEADER_USER_ID, HEADER_USERNAME
from rdm.config import Settings
from rdm.models import User

# --------------------------------------------------------------------------------------
# Mock provider
# --------------------------------------------------------------------------------------


def test_personas_are_the_four_local_users():
    assert list(PERSONAS) == ["admin", "function_admin", "editor", "viewer"]
    for key, persona in PERSONAS.items():
        assert isinstance(persona, Persona) and persona.key == key
        assert isinstance(persona.user, User)
        assert persona.label == persona.user.display_name
        assert persona.description
        assert "everyone" in persona.user.groups
    assert PERSONAS["admin"].user.groups == ("rdm_admins", "everyone")
    assert PERSONAS["function_admin"].user.groups == ("finance_admins", "student_readers", "everyone")
    assert PERSONAS["editor"].user.groups == ("student_stewards", "finance_readers", "everyone")
    assert PERSONAS["viewer"].user.groups == ("student_readers", "hr_readers", "everyone")
    assert PERSONAS["viewer"].user.email == "vera.viewer@example.org"


def test_mock_provider_defaults_to_admin():
    provider = MockAuthProvider()
    assert isinstance(provider, AuthProvider) and provider.name == "mock"
    assert provider.default_persona == "admin"
    assert provider.current_user() == PERSONAS["admin"].user
    assert provider.supports_persona_switching
    assert provider.personas() == list(PERSONAS.values())
    assert provider.access_token() is None


def test_mock_provider_honours_default_persona_and_falls_back_for_unknown():
    assert MockAuthProvider("editor").current_user() == PERSONAS["editor"].user
    assert MockAuthProvider("viewer").default_persona == "viewer"
    unknown = MockAuthProvider("superuser")
    assert unknown.default_persona == "admin"
    assert unknown.current_user() == PERSONAS["admin"].user


def test_mock_provider_persona_argument_overrides_default():
    provider = MockAuthProvider("editor")
    assert provider.current_user("viewer") == PERSONAS["viewer"].user
    assert provider.current_user("admin") == PERSONAS["admin"].user
    assert provider.current_user("nobody") == PERSONAS["editor"].user  # unknown -> default
    assert provider.current_user(None) == PERSONAS["editor"].user
    assert provider.current_user("") == PERSONAS["editor"].user


# --------------------------------------------------------------------------------------
# Databricks provider
# --------------------------------------------------------------------------------------


@pytest.fixture
def fake_groups(monkeypatch):
    calls: list[tuple[str, str | None]] = []

    def _groups_for(self, username, token):
        calls.append((username, token))
        return ("group_a", "group_b")

    monkeypatch.setattr(DatabricksAuthProvider, "_groups_for", _groups_for)
    return calls


def test_databricks_provider_reads_identity_headers(fake_groups):
    headers = {
        HEADER_EMAIL: "jane@example.org",
        HEADER_USERNAME: "jane",
        HEADER_USER_ID: "1234",
        HEADER_ACCESS_TOKEN: "tok-123",
    }
    provider = DatabricksAuthProvider(lambda: headers)
    assert provider.name == "databricks"
    assert not provider.supports_persona_switching and provider.personas() == []
    user = provider.current_user()
    assert user == User(
        username="jane", display_name="jane", groups=("group_a", "group_b"), email="jane@example.org"
    )
    assert provider.access_token() == "tok-123"
    assert fake_groups == [("jane", "tok-123")]
    assert provider.current_user("viewer") == user  # persona is ignored


def test_databricks_provider_falls_back_to_email_as_username(fake_groups):
    provider = DatabricksAuthProvider(lambda: {HEADER_EMAIL: "jane@example.org"})
    user = provider.current_user()
    assert user.username == "jane@example.org" and user.email == "jane@example.org"
    assert user.display_name == "jane@example.org"
    assert provider.access_token() is None
    assert fake_groups == [("jane@example.org", None)]


def test_databricks_provider_username_only(fake_groups):
    user = DatabricksAuthProvider(lambda: {HEADER_USERNAME: "svc"}).current_user()
    assert user.username == "svc" and user.email is None


@pytest.mark.parametrize(
    "headers", [{}, None, {HEADER_ACCESS_TOKEN: "tok"}, {HEADER_EMAIL: "", HEADER_USERNAME: ""}]
)
def test_databricks_provider_without_identity_headers_raises(fake_groups, headers):
    provider = DatabricksAuthProvider(lambda: headers)
    with pytest.raises(RuntimeError, match="No user identity headers"):
        provider.current_user()
    assert fake_groups == []


def test_databricks_provider_access_token_absent_or_blank(fake_groups):
    assert DatabricksAuthProvider(lambda: {}).access_token() is None
    assert DatabricksAuthProvider(lambda: {HEADER_ACCESS_TOKEN: ""}).access_token() is None
    assert DatabricksAuthProvider(lambda: None).access_token() is None


def test_databricks_provider_swallows_header_getter_errors(fake_groups):
    def boom():
        raise RuntimeError("no streamlit context")

    provider = DatabricksAuthProvider(boom)
    assert provider.access_token() is None
    with pytest.raises(RuntimeError, match="No user identity headers"):
        provider.current_user()


class _FakeGroup:
    def __init__(self, display):
        self.display = display


class _FakeMe:
    groups = [_FakeGroup("admins"), _FakeGroup(None), _FakeGroup("users")]


class _FakeCurrentUser:
    def me(self):
        return _FakeMe()


class _FakeUsers:
    last_call: dict = {}

    def list(self, **kw):
        _FakeUsers.last_call = kw
        return iter([_FakeMe()])


class _FakeWorkspaceClient:
    constructed: list[dict] = []

    def __init__(self, **kw):
        _FakeWorkspaceClient.constructed.append(kw)
        self.current_user = _FakeCurrentUser()
        self.users = _FakeUsers()


@pytest.fixture
def fake_sdk(monkeypatch):
    _FakeWorkspaceClient.constructed = []
    monkeypatch.setattr("databricks.sdk.WorkspaceClient", _FakeWorkspaceClient)
    return _FakeWorkspaceClient


def test_groups_for_with_user_token_uses_on_behalf_of_client_and_caches(fake_sdk, monkeypatch):
    provider = DatabricksAuthProvider(lambda: {HEADER_USERNAME: "jane", HEADER_ACCESS_TOKEN: "tok"})
    assert provider.current_user().groups == ("admins", "users")
    assert fake_sdk.constructed == [{"token": "tok", "auth_type": "pat"}]
    assert provider.current_user().groups == ("admins", "users")
    assert len(fake_sdk.constructed) == 1  # cached
    # a provider with an expired cache looks the groups up again
    monkeypatch.setattr("rdm.auth.provider.time.monotonic", lambda: 1e9)
    provider.current_user()
    assert len(fake_sdk.constructed) == 2


def test_groups_for_without_token_uses_service_principal_lookup(fake_sdk):
    provider = DatabricksAuthProvider(lambda: {HEADER_USERNAME: "jane"})
    assert provider.current_user().groups == ("admins", "users")
    assert fake_sdk.constructed == [{}]
    assert _FakeUsers.last_call == {"filter": "userName eq 'jane'", "attributes": "groups"}


def test_groups_for_lookup_failure_is_best_effort(monkeypatch):
    class Broken:
        def __init__(self, **kw):
            raise ConnectionError("no workspace")

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", Broken)
    provider = DatabricksAuthProvider(
        lambda: {HEADER_USERNAME: "jane", HEADER_ACCESS_TOKEN: "tok"}, group_cache_ttl=0
    )
    user = provider.current_user()
    assert user.groups == ()
    assert user.principals == {"jane"}


# --------------------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------------------


def test_settings_defaults_from_empty_env():
    s = Settings.from_env({})
    assert s == Settings()
    assert s.backend == "duckdb" and s.auth == "mock" and s.persona == "admin"
    assert s.duckdb_path == "data/rdm.duckdb" and s.catalog == "_reference_data"
    assert s.max_rows == 5000 and s.metadata_cache_ttl == 60
    assert not s.is_databricks
    assert s.databricks_host is None and s.warehouse_http_path is None


def test_settings_databricks_backend_defaults_auth_and_derives_http_path():
    s = Settings.from_env(
        {
            "RDM_BACKEND": " Databricks ",
            "DATABRICKS_HOST": "https://x.cloud.databricks.com",
            "DATABRICKS_WAREHOUSE_ID": "abc123",
        }
    )
    assert s.backend == "databricks" and s.is_databricks
    assert s.auth == "databricks"
    assert s.databricks_host == "https://x.cloud.databricks.com"
    assert s.databricks_warehouse_id == "abc123"
    assert s.databricks_http_path is None
    assert s.warehouse_http_path == "/sql/1.0/warehouses/abc123"


def test_settings_explicit_http_path_wins_over_warehouse_id():
    s = Settings.from_env(
        {"DATABRICKS_WAREHOUSE_ID": "abc123", "DATABRICKS_HTTP_PATH": "/sql/1.0/warehouses/custom"}
    )
    assert s.warehouse_http_path == "/sql/1.0/warehouses/custom"
    assert Settings(databricks_warehouse_id="w1").warehouse_http_path == "/sql/1.0/warehouses/w1"
    assert Settings(databricks_http_path="/p", databricks_warehouse_id="w1").warehouse_http_path == "/p"


def test_settings_parses_every_variable():
    env = {
        "RDM_BACKEND": "DUCKDB",
        "RDM_DUCKDB_PATH": "/tmp/x.duckdb",
        "RDM_AUTH": " Databricks",
        "RDM_PERSONA": "Editor ",
        "RDM_CATALOG": " main ",
        "RDM_MAX_ROWS": "250",
        "RDM_METADATA_CACHE_TTL": "5",
        "DATABRICKS_HOST": "",
        "DATABRICKS_WAREHOUSE_ID": "",
        "DATABRICKS_HTTP_PATH": "",
    }
    s = Settings.from_env(env)
    assert s == Settings(
        backend="duckdb",
        duckdb_path="/tmp/x.duckdb",
        auth="databricks",
        persona="editor",
        catalog="main",
        max_rows=250,
        metadata_cache_ttl=5,
    )
    assert s.databricks_host is None and s.databricks_warehouse_id is None and s.databricks_http_path is None


def test_settings_invalid_integer_raises():
    with pytest.raises(ValueError):
        Settings.from_env({"RDM_MAX_ROWS": "lots"})


def test_settings_from_process_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # no .env file here
    for key in (
        "RDM_BACKEND",
        "RDM_AUTH",
        "RDM_PERSONA",
        "RDM_MAX_ROWS",
        "DATABRICKS_WAREHOUSE_ID",
        "DATABRICKS_HTTP_PATH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RDM_PERSONA", "VIEWER")
    monkeypatch.setenv("RDM_MAX_ROWS", "42")
    s = Settings.from_env()
    assert s.persona == "viewer" and s.max_rows == 42 and s.backend == "duckdb"


def test_settings_reads_dotenv_file_without_overriding_environment(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("RDM_PERSONA=editor\nRDM_CATALOG=from_dotenv\n")
    for key in ("RDM_PERSONA", "RDM_CATALOG"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("RDM_PERSONA", "viewer")
    s = Settings.from_env()
    assert s.persona == "viewer"  # environment wins
    assert s.catalog == "from_dotenv"
