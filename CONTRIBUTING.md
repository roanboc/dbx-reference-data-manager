# Contributing

Thank you for helping to improve the Reference Data Manager. This page explains how the
repository is organised, how to run it and what a good change looks like.

## Set up

```bash
make install            # .venv with requirements-dev.txt (uv if available, else pip)
make seed               # local DuckDB with demo domains, functions, forms and files
make run                # http://localhost:8050 with hot reload
make browsers           # optional: Chromium for the Playwright smoke tests
make check              # ruff + pytest, what CI runs
```

Python 3.11 or later. Copy `.env.example` to `.env` for local settings; never commit `.env`.

## How the code is organised

- `src/rdm/models.py`, `coercion.py` - the domain model and value coercion shared by the
  grid, the imports and the backends.
- `src/rdm/backend/` - the only place SQL is allowed. `base.py` is the interface, DuckDB
  implements it for local use and tests, `databricks_backend.py` for the SQL warehouse.
- `src/rdm/services/` - role guards, navigation, drafts and change sets, Excel import, files.
- `src/rdm/ui/` - the Dash app: `layout.py` (shell), `routes.py` (addresses and their
  parsing), `app.py` (callbacks that render the page), `pages/`, `components.py` (shared
  building blocks: danger zone, dropzone, function card, stat tile, ...), `grid.py` (AG Grid
  configuration), `ids.py` (every component id), `help/` (in-app guides in Markdown).
- `docs/` - functional design, technical design, deployment; `resources/` and
  `databricks.yml` - the asset bundle.

## Conventions

- **No SQL in the UI or the services.** Add a method to `DatabaseBackend`, implement it in
  both backends, cover it with the DuckDB contract tests and the Databricks SQL-generation
  tests (`tests/test_databricks_backend.py`, fake connection).
- **Guards live in the service layer**, never only in the UI: hiding a button is not access
  control (see `docs/DESIGN.md` §11).
- **Component ids** go in `src/rdm/ui/ids.py`; pages never invent id strings. Addresses are
  built with the helpers in `src/rdm/ui/routes.py`.
- **Reuse the building blocks** in `components.py` (danger zone, dropzone, upload preview,
  function card, stat tile, role badges) and `grid.py` (preview grid, definition grids) rather
  than re-typing them in a page.
- **Settings** are added in four places together: `src/rdm/config.py`, `.env.example`,
  `app.yaml` and `resources/app.yml` (and the table in the README).
- **Colours** come from Mantine tokens (`var(--mantine-color-...)`) or are given for both
  colour schemes in `assets/styles.css`; no light-only hex values in Python.
- **Icons**: use `icon("tabler:<name>")`; after adding a new name run `make icons`
  (`scripts/vendor_icons.py`), which downloads the SVG and regenerates `assets/icons.css`.
  `tests/test_ui_icons.py` fails when an icon is missing.
- **User-facing behaviour** is documented in the in-app help (`src/rdm/ui/help/`) and, when
  it changes the design, in `docs/FUNCTIONAL_DESIGN.md` (requirement table) and
  `docs/DESIGN.md`.
- Style is enforced by ruff (`make lint`, `make format`); line length 110.

## Tests

| Layer | Where | Runs |
|---|---|---|
| Unit (models, coercion, drafts, import, SQL generation) | `tests/test_*.py` | always |
| Backend contract on DuckDB | `tests/test_duckdb_backend.py` | always |
| Dash server (layout, callbacks through the Flask client) | `tests/test_ui_app.py`, `test_ui_pages.py` | always |
| Browser smoke (colour scheme, icons, selection, item form) | `tests/test_browser.py` | when Chromium is available (`make browsers`, or `RDM_TEST_BROWSER=/path/to/chromium`) |
| Databricks SQL generation | `tests/test_databricks_backend.py` (fake connection) | always; a real workspace is walked through manually before a release (`docs/DEPLOYMENT.md` §7) |

## Screenshots

`make screenshots` regenerates `docs/screenshots/` from a running local app (`make seed`,
`make serve`, then the script). Keep the same file names so the README links stay valid.

## Pull requests

- One topic per pull request, with a short description of the why and how to verify.
- `make check` must pass; CI also runs the browser tests.
- Keep the change history readable: describe behaviour in the commit message, not files.
