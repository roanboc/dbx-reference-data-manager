# Reference Data Manager (SCD Manager) for Databricks

A Databricks App that replaces SharePoint lists as the place where business users maintain
reference data. Every "form" is a governed Delta table in Unity Catalog; every domain is a
schema; access is the schema's Unity Catalog grants. The app is metadata-driven: it discovers
domains and forms at runtime and renders editable grids from the table definitions.

* **Editable grid** (AG Grid): inline editing, sorting and filtering while editing, dropdowns
  for constrained values, cell-level validation highlighting, add/delete rows, one atomic save,
  optimistic concurrency (`_version`), CSV/Excel export, Excel/CSV row import.
* **Three roles per domain**: Viewer, Editor, Administrator, resolved from Unity Catalog
  privileges (locally from a mock persona switcher).
* **Administrator tools**: create a form from an Excel file (types inferred, adjustable),
  edit descriptions / required flags / business keys / allowed values, add and remove columns.
* **History**: row-level audit trail (who changed what, before/after) plus Delta Change Data
  Feed for downstream SCD Type 2 pipelines.
* **Repository pattern**: the UI never contains SQL. `DuckDBBackend` runs locally with zero
  infrastructure; `DatabricksBackend` runs on a SQL warehouse with on-behalf-of-user
  authorization.

Framework: **Dash + Dash AG Grid + Dash Mantine Components**. The rationale (versus
Streamlit, which was implemented first and is kept in git history) is in
[docs/FRAMEWORK_DECISION.md](docs/FRAMEWORK_DECISION.md).

## Quick start (local, DuckDB)

```bash
make install            # creates .venv and installs requirements-dev.txt
make seed               # creates data/rdm.duckdb with three demo domains
make run                # http://localhost:8050 (dev server with hot reload)
```

Switch persona in the header (Alice Admin / Eddie Editor / Vera Viewer) to see the
role-dependent navigation and controls. Settings come from `.env` (see `.env.example`).

```bash
make test               # pytest: unit, backend contract (DuckDB), Dash server smoke
make lint               # ruff
```

## Repository layout

```
app.py                    Dash entrypoint (python app.py --dev | python app.py = gunicorn)
app.yaml                  Databricks Apps runtime configuration
databricks.yml, resources/  Databricks Asset Bundle: catalog, domain schemas + grants, app
src/rdm/
  models.py               Domain model: DataType, ColumnDef, FormDef, DomainDef, Role, ChangeSet ...
  coercion.py             Value coercion shared by grid edits, imports and backends
  backend/base.py         DatabaseBackend interface (the only place SQL is allowed)
  backend/duckdb_backend.py      Local backend (emulates UC metadata in _rdm_meta)
  backend/databricks_backend.py  SQL warehouse backend (MERGE via from_json, audit table)
  auth/provider.py        MockAuthProvider (personas) / DatabricksAuthProvider (headers)
  services/               FormService, CatalogService, Draft (row-id edit tracking), Excel import
  ui/                     Dash app: shell, routing, pages, AG Grid configuration
tests/                    pytest suite (see docs/DESIGN.md §9 for the testing strategy)
docs/                     DESIGN.md, FRAMEWORK_DECISION.md, DEPLOYMENT.md
```

## How a save works

1. The grid loads rows keyed by `_id`; edits are tracked per row in a browser-side draft
   (`updates`, `inserts`, `deletes`) with the `_version` each row had when loaded.
2. Every edit is validated server-side (types, required, allowed values, unique business
   keys) and invalid cells are highlighted; Save is disabled until the draft is clean.
3. Save turns the draft into a `ChangeSet`; the DuckDB backend applies it in one transaction,
   the Databricks backend in one `MERGE` whose source is a single JSON parameter. Rows whose
   `_version` changed since loading are reported as conflicts and never overwritten.
4. Every change is written to the audit log (`_rdm_meta.change_log`) and shown in History.

## Notes

* Icons are loaded at runtime from the Iconify CDN (`dash-iconify`); in a network without
  internet access from the browser they simply do not render, the labels remain.
* AG Grid Community is used (no licence). Multi-cell clipboard paste is an AG Grid Enterprise
  feature; use *Import rows* for bulk changes.
* DuckDB allows a single writer process, so `python app.py` runs one gunicorn worker locally
  and `RDM_WORKERS` (default 2) against Databricks.

## Deploying to Databricks

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): `databricks bundle deploy` creates the catalog,
the domain schemas with their grants and the app; enable user authorization (`sql` scope)
so that queries run as the signed-in user and Unity Catalog enforces the roles.
