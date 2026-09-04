"""Application settings, read from environment variables (and a local ``.env`` file).

Production values are injected by Databricks Apps (see ``app.yaml``); local values come
from ``.env`` (see ``.env.example``).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # optional locally, present in requirements
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None  # type: ignore[assignment]

APP_TITLE = "Reference Data Manager"
APP_ICON = ":material/table_edit:"


@dataclass(frozen=True)
class Settings:
    backend: str = "duckdb"  # duckdb | databricks
    duckdb_path: str = "data/rdm.duckdb"
    auth: str = "mock"  # mock | databricks
    persona: str = "admin"  # default mock persona
    debug_personas: bool = False  # FR-45: mock personas with simulated roles (dev deployments)
    catalog: str = "_reference_data"
    max_rows: int = 5000
    max_file_mb: int = 200  # largest file accepted through the browser upload
    admin_contact: str = ""  # e-mail, URL or text shown in Help > About for other use cases
    databricks_host: str | None = None
    databricks_warehouse_id: str | None = None
    databricks_http_path: str | None = None
    metadata_cache_ttl: int = 300  # seconds for navigation metadata caching

    @property
    def is_databricks(self) -> bool:
        return self.backend == "databricks"

    @property
    def warehouse_http_path(self) -> str | None:
        if self.databricks_http_path:
            return self.databricks_http_path
        if self.databricks_warehouse_id:
            return f"/sql/1.0/warehouses/{self.databricks_warehouse_id}"
        return None

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        if env is None:
            if load_dotenv is not None:
                load_dotenv(Path(".env"), override=False)
            env = dict(os.environ)
        backend = env.get("RDM_BACKEND", "duckdb").strip().lower()
        auth = env.get("RDM_AUTH", "databricks" if backend == "databricks" else "mock").strip().lower()
        return cls(
            backend=backend,
            duckdb_path=env.get("RDM_DUCKDB_PATH", "data/rdm.duckdb"),
            auth=auth,
            persona=env.get("RDM_PERSONA", "admin").strip().lower(),
            debug_personas=env.get("RDM_DEBUG_PERSONAS", "").strip().lower() in {"1", "true", "yes"},
            catalog=env.get("RDM_CATALOG", "_reference_data").strip(),
            max_rows=int(env.get("RDM_MAX_ROWS", "5000")),
            max_file_mb=int(env.get("RDM_MAX_FILE_MB", "200")),
            admin_contact=env.get("RDM_ADMIN_CONTACT", "").strip(),
            databricks_host=env.get("DATABRICKS_HOST") or None,
            databricks_warehouse_id=env.get("DATABRICKS_WAREHOUSE_ID") or None,
            databricks_http_path=env.get("DATABRICKS_HTTP_PATH") or None,
            metadata_cache_ttl=int(env.get("RDM_METADATA_CACHE_TTL", "300")),
        )
