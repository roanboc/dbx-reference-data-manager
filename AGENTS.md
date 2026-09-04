# Agent guide — Reference Data Manager

Dash app on Databricks Apps for governed reference data (replaces SharePoint lists).
Hierarchy: **domain** (registry classifier) > **function** (Unity Catalog schema) >
**form** (Delta table) | **file** (CSV/Parquet in the function's `_files` volume).
Read this file first and only explore what it does not answer. Deep rationale and the
decision log live in [docs/DESIGN.md](docs/DESIGN.md).

## Commands

| Task | Command |
|---|---|
| Test | `python -m pytest` |
| Lint | `python -m ruff check src tests app.py scripts` |
| Format | `python -m ruff format src tests app.py scripts` |
| Run locally (DuckDB + mock auth) | `python app.py --dev` |
| Seed demo data | `python scripts/seed_demo.py` |

Use the project venv (`.venv`); the Makefile wraps the same targets (`make check` = lint + test).

## Architecture (layers; dependencies point downward only)

| Layer | Module | Role |
|---|---|---|
| UI | `src/rdm/ui/` | Dash shell. `app.py` routing, `context.py` per-request `AppContext` + caches, `ids.py` **all** component ids, `pages/*` one module per page |
| Services | `src/rdm/services/` | Role guards + orchestration: `form_service.py` (validation, ChangeSet), `catalog_service.py` (navigation), `draft.py` (pending grid edits), `excel_import.py`, `files.py` |
| Auth | `src/rdm/auth/provider.py` | Strategy: `MockAuthProvider` (personas) / `DatabricksAuthProvider` (request headers) |
| Backends | `src/rdm/backend/` | `base.py` = `DatabaseBackend` ABC (repository, all errors are `BackendError` subclasses), `duckdb_backend.py` local, `databricks_backend.py` production, `factory.py` builds from `Settings`, `sql_utils.py` shared quoting/type maps |
| Domain model | `src/rdm/models.py`, `coercion.py` | Dataclasses, naming rules, value coercion. Knows no SQL and no Dash |

## Invariants (do not violate)

- **No SQL outside `rdm.backend`.** UI and services only call `DatabaseBackend` methods.
- **Authorization** is decided in the service layer (role guards) and re-enforced by Unity
  Catalog in production (statements run as the signed-in user). Backends only audit-stamp
  with the acting `User`.
- **Backend divergence is deliberate.** DuckDB applies changes row-by-row in one
  transaction; Databricks submits one atomic `MERGE` with a JSON payload, and file I/O is
  local filesystem vs Files API. Do **not** unify `apply_changes`, `_append_rows` or file
  handling into a shared template — keeping dialects isolated is a documented decision.
- Every form carries system columns `_id`, `_version`, `_created_at/by`, `_updated_at/by`
  (`models.py`); saves are optimistic-concurrency checked on `_version`.
- **Metadata is mandatory** (FR-40/41): `require_metadata` in `form_service.py` demands a
  description and an owner on functions, forms and files (optional `owner_email`); new
  columns need descriptions. Keep the guard in the service layer.
- Identifiers are lower_snake_case, max 63 chars, no leading `_` for user objects —
  always go through `validate_identifier` / `sanitize_identifier` (`models.py`).
- New settings: add to `Settings` + `Settings.from_env` (`config.py`), env var prefix `RDM_`.
- Dash component ids exist only in `ui/ids.py` (helpers there for pattern-matching ids).
- The Databricks backend refuses statements/paths outside the reference-data catalog
  (confinement guard); never weaken it.
- **SCD2 history tables** (FR-47, optional per form): `_h__<form>` in the form's schema,
  Auto CDC notation `__START_AT`/`__END_AT` (current row `__END_AT IS NULL`). Windows
  are closed/opened inside each backend's `apply_changes`/`_append_rows`; tables whose name
  starts with `_` are app-managed and excluded from form listings and counts. The window
  columns are app constants — quote them literally, never through `validate_identifier`.

## Established idioms (copy these; do not invent new ones)

- **Mutation callback** (canonical example: `save_settings` in `ui/pages/form_page.py`):
  1. `if not n: return no_update...`
  2. validate input → `notify(msg, color="yellow")` for user mistakes
  3. `c = get_context(persona)`
  4. `try:` service call `except (BackendError, PermissionDenied, ValueError) as exc:` →
     `notify(str(exc), color="red")` or `error_alert(exc)`
  5. on success: `invalidate_metadata()` + `notify(...)` + bump the relevant version store
     (`GRID_VERSION` / `NAV_VERSION`).
  6. add `running=[(Output(<submit id>, "loading"), True, False)]` to the decorator so the
     button spins and cannot double-submit (FR-38).
- **Destructive operations** require typed confirmation: `(confirm or "").strip() != target`.
- **Colour scheme** (FR-37): the header `SegmentedControl` (light / dark only) persists itself
  (`persistence_type="local"`); one clientside callback seeds it from
  `prefers-color-scheme` on first visit and another maps it to the provider's
  `forceColorScheme`. Dark CSS keys off
  `[data-mantine-color-scheme="dark"]`, never `prefers-color-scheme`.
- **Import modes** (FR-43) are built in the service (`build_import_changeset`) on the
  ChangeSet machinery — merge/replace never added backend methods; keep it that way.
- **New registry column?** Add it to both DDLs, DuckDB `_migrate_meta` (ALTER only when the
  table pre-exists), and rely on the Databricks `_registry_upsert` self-heal (`ADD COLUMNS`
  on the unresolved-column error). Registry reads use `SELECT *` and stay schema-tolerant.
- Page modules expose `render(ctx, ...)` and `register(app)`; both are wired in
  `ui/app.py` (`parse_path` for routes). Sections use `# -- name ----` comment headers.
- Backend contract change = update `base.py` ABC + **both** backends + both test suites.

## Testing conventions

- DuckDB backend is exercised for real (`test_duckdb_backend.py`); Databricks via mocked
  connections asserting generated SQL (`test_databricks_backend.py`);
  `test_databricks_live.py` only runs with real credentials (opt-in).
- UI: `test_ui_pages.py` renders pages headlessly; `test_ui_app.py` smoke-tests the server.
- Fixtures live in `tests/conftest.py`.
