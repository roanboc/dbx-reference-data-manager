"""Settings from the environment (rdm.config)."""

from __future__ import annotations

import pytest

from rdm.config import Settings


def test_admin_contact_from_env_is_optional_and_stripped():
    assert Settings.from_env({}).admin_contact == ""
    assert Settings.from_env({"RDM_ADMIN_CONTACT": "  data-office@example.org "}).admin_contact == (
        "data-office@example.org"
    )


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
        "RDM_MAX_FILE_MB": "50",
        "RDM_ADMIN_CONTACT": " data-office@example.org ",
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
        max_file_mb=50,
        admin_contact="data-office@example.org",
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
