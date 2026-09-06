# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added

- Light and dark colour schemes with a System / Light / Dark control in the header. The
  choice follows the operating system by default (light when it cannot be determined) and
  is remembered per browser. Grids, help pages and every custom style follow the scheme.
- Icons are vendored Tabler SVGs (`assets/icons/`, `scripts/vendor_icons.py`); the app no
  longer fetches anything from a CDN at runtime.
- Browser smoke tests with Playwright (`tests/test_browser.py`), run by CI, and a
  screenshot script (`scripts/screenshots.py`) for the README.
- Repository hygiene for GitHub: licence (MIT), contributing guide, code of conduct,
  security policy, issue and pull request templates, this changelog.

### Changed

- README rewritten around the business case and the governance model, with screenshots.
- Final clean-up before the Databricks release: the Streamlit-era positional change-set
  builder and other dead code are gone; the two backends share identifier quoting, search and
  ordering, conflict messages and history decoding; the pages share one danger zone, dropzone,
  upload preview, function card and stat tile; addresses live in `ui/routes.py`.
- Consistency between the backends: audit entries are kept when a form is dropped, the file
  count is the registry count, the Databricks backend reports a failed history write as a
  warning after a save instead of hiding it, and legacy `.xls` uploads are no longer offered.

### Fixed

- Whole numbers above 2^53 typed as text or Decimal were rounded through `float()`.
- An edited row that duplicated a business key of a row loaded *later* was not reported.
- A malformed stored column configuration raised instead of being ignored.
- The wizard's Source step swallowed its own validation messages ("Upload a file to continue").
- The function danger zone counted files as forms; the Domains page rendered two components
  with the same id; exporting to Excel had no error handling.
- AG Grid row selection uses the current object API (`rowSelection`, `selectionColumnDef`);
  the deprecated `checkboxSelection` / `suppressRowClickSelection` options are gone.
- Hard-coded light-only colours replaced by Mantine tokens; one grid CSS hook (`rdm-grid`)
  instead of a theme class repeated in four pages.

## [0.1.0] - 2026-09-02

First complete implementation: domain > function > form | file hierarchy, editable grid with
validation and one atomic save, item form with per-row history and restore, bulk update,
Excel/CSV import and export, files (CSV/Parquet) in a volume per function, form creator
wizard, schema editor, roles from Unity Catalog grants, registry and audit trail, DuckDB
backend for local development, Databricks SQL backend with catalog confinement, asset bundle
and CI. The earlier Streamlit implementation is kept in git history
(see `docs/FRAMEWORK_DECISION.md`).
