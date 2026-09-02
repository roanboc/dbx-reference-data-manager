# Reference Data Manager (SCD Manager) — Design

This document is the architecture and decision log for the Databricks Reference Data
Stewardship App. It is written so that a reviewer can challenge the decisions before
the production (Databricks) backend is switched on.

## 1. Goals and non-goals

**Goals**

* One generic Streamlit app that manages every "CRUD list" and slowly-changing reference
  table of the platform, replacing SharePoint lists.
* Data lives in Unity Catalog Delta tables (`<catalog>.<domain>.<form>`), governance is
  Unity Catalog grants at schema level, infrastructure is a Databricks Asset Bundle.
* The UI never contains SQL. Every database interaction goes through a `DatabaseBackend`
  interface with a DuckDB implementation for local development and a Databricks SQL
  implementation for production.
* Three UI tiers: Administrator, Editor (User), Viewer.

**Non-goals**

* Editing tables with millions of rows. Reference lists are small (hundreds to tens of
  thousands of rows). The grid loads a bounded page (`RDM_MAX_ROWS`, default 5,000) with
  server-side search.
* Replacing pipelines that build SCD Type 2 dimensions. The app owns the *current state* of
  a list plus its full change history; Type 2 dimensions are derived downstream (see §7).

## 2. Repository layout

```
app.py                      Streamlit entrypoint (thin: wiring + page routing)
app.yaml                    Databricks Apps runtime config
databricks.yml              Databricks Asset Bundle (catalog, schemas, grants, app, setup job)
resources/                  Bundle resource files
src/rdm/
  config.py                 Settings from environment (.env for local)
  models.py                 DataType, ColumnDef, FormDef, DomainDef, User, Role, ChangeSet, ...
  backend/base.py           DatabaseBackend abstract interface
  backend/duckdb_backend.py Local backend (DuckDB file, emulates UC metadata in _rdm_meta)
  backend/databricks_backend.py  Databricks SQL warehouse backend (databricks-sql-connector)
  backend/sql_utils.py      Identifier validation/quoting, type maps
  backend/factory.py        Backend + auth selection from settings
  auth/provider.py          AuthProvider: MockAuthProvider (personas), DatabricksAuthProvider (headers)
  services/catalog_service.py   Navigation model: visible domains/forms, search
  services/form_service.py      Grid data, change-set building, validation, save
  services/excel_import.py      Excel/CSV parsing, type inference, column sanitising
  ui/                       Streamlit views (sidebar, grid, form creator, schema editor, history)
tests/                      Unit, contract (backend-parametrised) and AppTest UI tests
scripts/seed_demo.py        Creates the local DuckDB file with demo domains/forms
docs/                       This document + deployment notes
```

## 3. Domain model

| Concept | Local (DuckDB) | Production (Unity Catalog) |
|---|---|---|
| Catalog | DuckDB database file | `_forms` (configurable `RDM_CATALOG`) |
| Domain | schema | schema, e.g. `student__survey_service_improvement` |
| Form | table | Delta table |
| Form description | `COMMENT ON TABLE` | `COMMENT ON TABLE` |
| Column description | `COMMENT ON COLUMN` | column `COMMENT` |
| Display name, owner, column config | `_rdm_meta.object_properties` | `TBLPROPERTIES ('rdm.display_name', 'rdm.owner', 'rdm.column_config')` + tags `rdm_display_name`, `rdm_owner` |
| Domain description/owner | `_rdm_meta.object_properties` (DuckDB cannot comment schemas) | `COMMENT ON SCHEMA` + schema tags |
| Change history | `_rdm_meta.change_log` | Delta Change Data Feed + `DESCRIBE HISTORY` |
| Permissions | `_rdm_meta.grants` (persona groups) | UC schema privileges via `information_schema` |

### 3.1 Portable data types

The app exposes a deliberately small type set so that a form created locally is created
identically in Databricks:

| RDM type | DuckDB | Databricks | Grid editor |
|---|---|---|---|
| `STRING` | `VARCHAR` | `STRING` | TextColumn / SelectboxColumn when options are defined |
| `INTEGER` | `BIGINT` | `BIGINT` | NumberColumn(step=1) |
| `DECIMAL(p,s)` | `DECIMAL(p,s)` | `DECIMAL(p,s)` | NumberColumn(format) |
| `DOUBLE` | `DOUBLE` | `DOUBLE` | NumberColumn |
| `BOOLEAN` | `BOOLEAN` | `BOOLEAN` | CheckboxColumn |
| `DATE` | `DATE` | `DATE` | DateColumn |
| `TIMESTAMP` | `TIMESTAMP` | `TIMESTAMP` | DatetimeColumn |

Unknown native types (tables created outside the app) are surfaced as `OTHER`, shown
read-only in the grid, and never silently converted.

### 3.2 System columns (SharePoint-style)

Every form created by the app carries hidden/read-only system columns:

| Column | Type | Purpose |
|---|---|---|
| `_id` | STRING, NOT NULL, PK | Stable row identity (UUID). Required so grid edits map to `UPDATE/DELETE ... WHERE _id = ?`. |
| `_created_at`, `_created_by` | TIMESTAMP, STRING | Audit |
| `_updated_at`, `_updated_by` | TIMESTAMP, STRING | Audit and optimistic concurrency token |

Business keys (e.g. `cost_centre_code`) are optional column flags used for uniqueness
validation before save; `_id` remains the technical key. This keeps SharePoint's familiar
"ID / Created / Modified / Modified By" semantics and makes tables safe to edit concurrently.

## 4. `DatabaseBackend` interface (summary)

```
# metadata
list_domains() -> list[DomainDef]
get_domain(name) -> DomainDef
create_domain(DomainDef)                       # admin
list_forms(domain) -> list[FormDef]            # cheap: names, description, display name, owner
get_form(domain, name) -> FormDef              # full: columns, properties, tags, row count
create_form(FormDef, rows: DataFrame | None)   # admin
update_form_metadata(FormDef)                  # description, display name, owner, column config
add_column / drop_column / set_column_description
drop_form(domain, name)                        # admin
# data
read_rows(FormDef, search: str | None, limit: int) -> DataFrame   (includes system columns)
apply_changes(FormDef, ChangeSet, actor: User) -> SaveResult      (inserts/updates/deletes, conflicts)
append_rows(FormDef, DataFrame, actor) -> int
get_history(FormDef, limit) -> DataFrame       # standard columns: version, changed_at, changed_by, change_type, row columns
# authorisation
get_domain_roles(user) -> dict[str, Role]      # effective role per domain for this user
grant_domain_role(domain, principal, role)     # admin; UC: GRANT ... ON SCHEMA
```

Design rules:

* Identifiers are validated with a strict regex (`^[a-z][a-z0-9_]{0,62}$`) at the model
  layer and quoted by the backend; values are always bound as parameters, never interpolated.
* All writes for one "Save" are applied as a batch: DuckDB uses a transaction; Databricks
  uses one `INSERT ... VALUES`, one `MERGE` (updates) and one `DELETE` per save, chunked to
  respect the 255-parameter limit of native parameters. Delta guarantees atomicity per
  statement; the UI reports partial failures explicitly.
* Updates and deletes carry the row's `_updated_at` seen at load time
  (`WHERE _id = ? AND _updated_at IS NOT DISTINCT FROM ?`). A mismatch is reported as a
  conflict ("row changed by someone else") instead of overwriting.

## 5. Authentication and authorisation

* `AuthProvider.current_user() -> User(email, display_name, groups)`.
  * Local: `MockAuthProvider` with three personas (sidebar switcher or `RDM_PERSONA` env var).
  * Databricks: `DatabricksAuthProvider` reads `st.context.headers`
    (`X-Forwarded-Email`, `X-Forwarded-Preferred-Username`, `X-Forwarded-User`,
    `X-Forwarded-Access-Token`).
* **Recommended production mode: user authorization (on-behalf-of-user) with the `sql`
  scope.** Every SQL statement then runs *as the signed-in user*, so Unity Catalog is the
  real enforcement point. The app's role logic only decides what to render.
* Role derivation from UC privileges on the schema (or inherited from the catalog):

| Role | Unity Catalog privileges on the domain schema |
|---|---|
| Viewer | `USE CATALOG`, `USE SCHEMA`, `SELECT` |
| Editor | Viewer + `MODIFY` |
| Administrator | Editor + `CREATE TABLE` + `MANAGE` (or schema ownership) |

  Grants are given to groups in the bundle (`resources.schemas.<x>.grants`). Tables created
  by the app are re-owned to the schema owner group so every administrator can alter them.
* Fallback (service-principal mode, no OBO): the app still resolves roles from UC grants
  for the user's groups, but the service principal must hold the union of privileges and
  the app becomes the security boundary. Documented, but not recommended.

## 6. Grid editing model

1. `read_rows` returns a DataFrame including `_id` and `_updated_at` (hidden via
   `column_config`).
2. `st.data_editor(num_rows="dynamic", key=<form>-<version>)` renders; the widget state
   (`edited_rows`, `added_rows`, `deleted_rows`) is positional.
3. `FormService.build_changeset(snapshot_df, editor_state, form)` resolves positions to
   `_id`s, coerces values to the column type, and validates (required, options, unique
   business keys, type). Issues are shown inline; Save is disabled until they are fixed.
4. Save calls `backend.apply_changes`, shows a summary (inserted/updated/deleted/conflicts),
   bumps the editor version (which clears widget state) and reloads.
5. While there are pending changes the search box and persona switcher are locked, because
   changing the displayed rows would invalidate positional edits.

## 7. History and slowly changing dimensions

* The grid is the **current state** (SCD Type 1 semantics) of a reference list.
* Every save stamps `_updated_at/_updated_by`; the History tab shows row-level changes:
  * DuckDB: `_rdm_meta.change_log` (before/after JSON per row, batch id, actor).
  * Databricks: Delta Change Data Feed (`table_changes(...)`) joined with `DESCRIBE HISTORY`
    for the operation and user. CDF is enabled at table creation
    (`delta.enableChangeDataFeed = true`).
* Type 2 dimensions should be built downstream from CDF (Lakeflow Declarative Pipelines
  `AUTO CDC ... STORED AS SCD TYPE 2`), not maintained by hand in the grid.
* Optional "effective-dated" form template adds `valid_from`, `valid_to`, `is_current`
  business columns for lists whose changes must be authored ahead of time.

## 8. Admin features

* **Form creator (Excel upload)**: sheet selection → inferred columns (name sanitised, type,
  description, required, business key, allowed values) editable in a grid → domain/table
  name/display name/description/owner → create table (+ system columns, comments,
  properties, tags) and optionally load the rows.
* **Schema editor**: edit descriptions, add/drop columns, edit allowed values, business
  keys, owner and display name. Type changes are intentionally not offered (Delta requires
  a rewrite); the guided path is "add a column, migrate, drop the old one".
* **Domain creator**: new schema with description and owner (requires catalog-level admin).

## 9. Local development and testing strategy

DuckDB is the local *runtime* backend; it is not the whole testing story:

| Layer | What | Runs where |
|---|---|---|
| Unit | change-set diffing, validation, type inference, identifier rules, SQL generation of the Databricks backend against a fake cursor | every commit, milliseconds |
| Contract | one parametrised suite executed against **every** backend (`DuckDBBackend` always; `DatabricksBackend` against a dev catalog when `RDM_TEST_DATABRICKS=1`) | local + CI job in the workspace |
| UI | Streamlit `AppTest` (headless, no browser) for persona flows | every commit |
| Visual | Playwright smoke for screenshots | on demand |
| Real | `databricks apps run-local` against a dev workspace (injects the `X-Forwarded-*` headers) | before release |

## 10. Databricks Apps practices applied

* `app.yaml` at repo root; `command: ["streamlit", "run", "app.py"]`; warehouse injected
  via `valueFrom: sql-warehouse` → `DATABRICKS_WAREHOUSE_ID`; no secrets in code.
* Do not override `STREAMLIT_SERVER_PORT`/`STREAMLIT_SERVER_ADDRESS`; the runtime sets
  them. `STREAMLIT_BROWSER_GATHER_USAGE_STATS=false`.
* Connections are cached per user token with `st.cache_resource(ttl=...)`; metadata reads
  cached with `st.cache_data` and an explicit "Refresh" action.
* Native parameter binding, identifier validation, least-privilege service principal
  (only `CAN_USE` on the warehouse; data access through user authorization).
* Serverless SQL warehouse recommended (cold-start latency dominates UX otherwise).
* Everything (catalog, schemas, grants, app, setup job) is declared in the bundle.
