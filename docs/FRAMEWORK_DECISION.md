# ADR-001: UI framework - Dash (with AG Grid) over Streamlit

**Status:** accepted · **Date:** 2026-09-02 · **Supersedes:** the Streamlit implementation
kept in git history (commit `8084ffc`, "Checkpoint: Streamlit implementation").

## Context

The Reference Data Manager is a metadata-driven CRUD platform: it discovers Unity Catalog
schemas and tables at runtime, renders grids and forms from column metadata, enforces
schema-level roles resolved from the user's identity (on-behalf-of-user SQL), lets
administrators create forms from Excel, and must scale to hundreds of forms across
business domains. The brief asked for Streamlit; a first implementation was built with it
and is complete (see the checkpoint commit). Feedback then asked whether Dash is the
better long-term foundation. Both frameworks are first-class on Databricks Apps, and the
decision is made on architecture, not on initial effort or verbosity (most code is
AI-assisted).

The backend and service layers (`rdm.models`, `rdm.backend`, `rdm.services`, `rdm.auth`)
contain no UI code, so the decision only affects `rdm.ui` and `app.py`.

## Assessment against the long-term requirements

| Requirement | Streamlit (rerun model, `st.data_editor`) | Dash (callbacks, Dash AG Grid) |
|---|---|---|
| **Complex UI state** | Everything lives in `session_state` and is rebuilt by a top-to-bottom rerun. Widget identity is `key` + structure; the editor keeps *positional* edit state, so the displayed frame has to be pinned per "version", and search/sort/navigation must be locked while edits are pending. Fragments reduce cost but not the coupling. | State is explicit: `dcc.Store` components hold the draft, the grid holds row data keyed by `_id` (`getRowId`), and each interaction is a callback with declared inputs/outputs. No positional mapping, no locks; a search can run while a draft exists. |
| **Event handling** | There are no events, only "the script ran again and a widget value differs". Cell-level events (which row, old vs new value) must be inferred by diffing. | `cellValueChanged` delivers row id, column, old and new value; `selectedRows`, `rowTransaction`, `cellClicked` are first-class. Business rules attach to events, not to diffs. |
| **Editable grid capabilities** | Excellent Excel-style multi-cell paste and a compact API. But: options per column are static (no per-row lookups), no cell-level validation styling, sorting is disabled while rows can be added, hidden columns can be re-shown by the user, `required=True` silently drops half-filled rows, and `AppTest` cannot drive the editor at all. | AG Grid Community: per-cell editors (`agSelectCellEditor` with values computed per row, number/date/checkbox editors), `cellClassRules` for validation highlighting, tooltips, pinned columns, sorting/filtering while editing, row selection, undo/redo, CSV export, infinite/server-side row model for large tables. Multi-cell clipboard paste is an AG Grid *Enterprise* feature; the community build pastes single cells, so bulk changes come through the Excel import path. |
| **Validation** | Server-side only, reported as a list under the grid. Client-side `required`/`validate` exist but interact badly with dynamic rows. | Client rules in `columnDefs` (editor params, `valueParser`, `cellClassRules`) plus server validation returning per-cell issues that the grid can render in place. |
| **Dynamic component generation** | Trivial: render whatever the metadata says on each rerun. | Pattern-matching callbacks (`MATCH`/`ALL`) and layout functions generate components per form; slightly more ceremony, but the callback graph stays static. |
| **Permissions-driven behaviour** | Conditional rendering per rerun; identity via `st.context.headers`. | Conditional layout in callbacks; identity via `flask.request.headers` on every request, which maps naturally to on-behalf-of-user tokens (one connection per user, no thread-shared widget state). |
| **Scale (hundreds of forms)** | One process, one script; navigation for hundreds of forms is a hand-built searchable tree of buttons. | URL routing (`/f/<domain>/<form>`) gives every form a shareable address and lazy loading; `dash.page_registry` or a router callback handles hundreds of forms without rendering them all. |
| **Extensibility: lookup fields, dependent dropdowns, bulk edit, per-row history, custom validation, SharePoint-like item forms** | Each of these hits a wall in `st.data_editor` (no per-row options, no row actions, no cell styling). They would require custom React components or leaving the grid. | All are AG Grid/Dash features: cell editor params per row, `cellValueChanged` → dependent recalculation, `selectedRows` bulk actions, row-click → modal with history, validation classes. |
| **Maintainability and testing** | Less code, but implicit control flow; behaviour depends on rerun order and widget-key discipline; the grid cannot be unit-tested. | More code, but callbacks are plain functions (unit-testable without a browser), the layout is data, and `dash.testing`/Playwright cover the browser. |
| **Databricks fit** | Officially supported; `st.cache_resource` for connections. | Officially supported; Flask/gunicorn, request-scoped identity, `dash-ag-grid` is what Databricks' own write-back example uses (not weighted, but it confirms the stack is exercised on the platform). |

**Where Streamlit wins:** speed of building simple views, Excel-like range paste, fewer
concepts. **Where Dash wins:** explicit state and events, a grid that grows with the
requirements, identity-keyed editing, testability, routing, and enterprise UI control
(Mantine components: app shell, stepper, modals, notifications).

## Decision

Use **Dash** with **Dash AG Grid** (community) and **Dash Mantine Components** for the UI.
The repository-pattern core is kept unchanged. The Streamlit implementation stays in git
history as a reference and as evidence that the backend abstraction is framework-agnostic.

Consequences:

* Grid edits are tracked by row id in a draft store (`updates`, `inserts`, `deletes`);
  the same `ChangeSet` model and `apply_changes` contract are reused.
* Multi-cell paste is not available in the community grid; bulk changes go through the
  Excel/CSV import, and single-cell paste, fill-down and keyboard navigation remain.
* The app runs under gunicorn (`app.yaml`), which allows several workers because no
  server-side widget state is kept between requests; caches are per process.
* Local development keeps DuckDB and the persona switcher; callbacks are unit-tested by
  calling them directly, and Playwright drives Chromium for the smoke tests in
  `tests/test_browser.py`.
