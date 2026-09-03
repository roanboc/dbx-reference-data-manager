# Reference Data Manager (SCD Manager) for Databricks

A Databricks App that replaces SharePoint lists as the place where business users maintain
reference data. The hierarchy is **domain > function > form | file**: a domain is a business
classifier aligned with the organisation's data domains, a function is a Unity Catalog
schema, every "form" is a governed Delta table in that schema, and every "file" is a CSV or
Parquet dataset in the schema's `_files` volume (for lists too large to edit in a grid).
Access is the schema's Unity Catalog grants. The app is metadata-driven: it discovers
domains, functions, forms and files at runtime and renders editable grids from the table
definitions. The catalog is `_reference_data`.

* **Editable grid** (AG Grid): inline editing, sorting and filtering while editing, dropdowns
  for constrained values, cell-level validation highlighting, add/delete rows, bulk update of
  selected rows, one atomic save, optimistic concurrency (`_version`), CSV/Excel export,
  Excel/CSV row import.
* **Item form**: one row in a dialog for wide lists, with the row's own history and a
  *Restore* per version; deleted rows are restored from the History tab.
* **Files**: CSV/Parquet datasets (thousands to millions of rows) kept as files in a volume
  per function, with preview, inferred columns, download, replace and upload history; files
  landed by pipelines appear automatically.
* **Domain overview**: one page per domain with its functions, forms and files; every form
  and file shows its Databricks path (`catalog`.`schema`.`object`, volume path) and copy-ready
  query snippets.
* **Safe deletes**: only global admins delete, with a typed confirmation; nothing cascades and
  the Databricks backend refuses any statement or file path outside the `_reference_data`
  catalog (docs/DESIGN.md §11).
* **Roles**: Viewer, Editor and Function admin per function, plus Global admin at catalog
  level (creates functions, maintains the domain list, deletes functions and forms, sees the
  administration guide). Resolved from Unity Catalog privileges; locally from a mock persona
  switcher with one persona per role. Access is granted to groups only.
* **Administrator tools**: create a form from an Excel file (types inferred, adjustable),
  edit descriptions / required flags / business keys / allowed values, add and remove columns.
* **History**: row-level audit trail (who changed what, before/after) plus Delta Change Data
  Feed for downstream SCD Type 2 pipelines.
* **Registry**: `_catalog.domains`, `_catalog.functions`, `_catalog.forms` and
  `_catalog.files` record every domain, function, form and file with display name,
  description, owner and documentation link.
* **Help**: in-app user guide, form-building guide and (for global admins) the Databricks
  administration guide.
* **Repository pattern**: the UI never contains SQL. `DuckDBBackend` runs locally with zero
  infrastructure; `DatabricksBackend` runs on a SQL warehouse with on-behalf-of-user
  authorization.

Start with [docs/FUNCTIONAL_DESIGN.md](docs/FUNCTIONAL_DESIGN.md) for the business, data and
process view. Framework: **Dash + Dash AG Grid + Dash Mantine Components**. The rationale (versus
Streamlit, which was implemented first and is kept in git history) is in
[docs/FRAMEWORK_DECISION.md](docs/FRAMEWORK_DECISION.md).

## Quick start (local, DuckDB)

```bash
make install            # creates .venv and installs requirements-dev.txt
make seed               # creates data/rdm.duckdb (+ data/files/) with demo domains, functions, forms and files
make run                # http://localhost:8050 (dev server with hot reload)
```

Switch persona in the header (Alice Admin / Fiona Function-admin / Eddie Editor / Vera
Viewer) to see the role-dependent navigation and controls. Settings come from `.env` (see
`.env.example`). A `data/rdm.duckdb` created before domains and functions were separated is
upgraded on first open; `python scripts/seed_demo.py --reset` recreates the demo content.

```bash
make test               # pytest: unit, backend contract (DuckDB), Dash server smoke
make lint               # ruff
```

## Repository layout

```
app.py                    Dash entrypoint (python app.py --dev | python app.py = gunicorn)
app.yaml                  Databricks Apps runtime configuration
databricks.yml, resources/  Databricks Asset Bundle: catalog, function schemas + grants, app
src/rdm/
  models.py               Domain model: DataType, ColumnDef, FormDef, FunctionDef, DomainDef, Role, ChangeSet ...
  coercion.py             Value coercion shared by grid edits, imports and backends
  backend/base.py         DatabaseBackend interface (the only place SQL is allowed)
  backend/duckdb_backend.py      Local backend (emulates UC metadata in _catalog)
  backend/databricks_backend.py  SQL warehouse backend (MERGE via from_json, audit table)
  auth/provider.py        MockAuthProvider (personas) / DatabricksAuthProvider (headers)
  services/               FormService, CatalogService, Draft (row-id edit tracking, bulk update,
                          restore), Excel import, file upload helpers
  ui/                     Dash app: shell, routing, pages (home, function, domains, form, file,
                          wizard, help), AG Grid configuration
tests/                    pytest suite (see docs/DESIGN.md §9 for the testing strategy)
docs/                     FUNCTIONAL_DESIGN.md (business, data, process), DESIGN.md (technical),
                          FRAMEWORK_DECISION.md, DEPLOYMENT.md
```

## How a save works

1. The grid loads rows keyed by `_id`; edits are tracked per row in a browser-side draft
   (`updates`, `inserts`, `deletes`) with the `_version` each row had when loaded. Cell
   edits, added and deleted rows, bulk updates, item-form edits and restored versions all
   land in that draft, which survives switching between the form's tabs.
2. Every edit is validated server-side (types, required, allowed values, unique business
   keys) and invalid cells are highlighted; Save is disabled until the draft is clean.
3. Save turns the draft into a `ChangeSet`; the DuckDB backend applies it in one transaction,
   the Databricks backend in one `MERGE` whose source is a single JSON parameter. Rows whose
   `_version` changed since loading are reported as conflicts and never overwritten.
4. Every change is written to the audit log (`_catalog.change_log`) and shown in History,
   both per form and per row.

## Notes

* Icons are loaded at runtime from the Iconify CDN (`dash-iconify`); in a network without
  internet access from the browser they simply do not render, the labels remain.
* AG Grid Community is used (no licence). Multi-cell clipboard paste is an AG Grid Enterprise
  feature; use *Bulk update* or *Import rows* for bulk changes.
* DuckDB allows a single writer process, so `python app.py` runs one gunicorn worker locally
  and `RDM_WORKERS` (default 2) against Databricks.

## Deploying to Databricks

See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md): `databricks bundle deploy` creates the catalog,
the function schemas with their grants and the app; enable user authorization (`sql` scope)
so that queries run as the signed-in user and Unity Catalog enforces the roles. Domains are
created in the app by a global admin (**Domains** in the sidebar) and referenced from the
bundle through the `rdm.domain` schema property.
