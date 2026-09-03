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
