# Reference Data Manager (SCD Manager) — Design

This document is the architecture and decision log for the Databricks Reference Data
Stewardship App. It is written so that a reviewer can challenge the decisions before the
production (Databricks) backend is switched on. The UI framework decision is recorded
separately in [FRAMEWORK_DECISION.md](FRAMEWORK_DECISION.md) (Dash + AG Grid; the earlier
Streamlit implementation is kept in git history).

## 1. Goals and non-goals

**Goals**

* One generic web application that manages every "CRUD list" and slowly changing reference
  table of the platform, replacing SharePoint lists.
* Data lives in Unity Catalog Delta tables (`<catalog>.<function>.<form>`) and, for datasets
  too large for a grid, in files of the volume `<catalog>.<function>._files`; governance is
  Unity Catalog grants at schema level, infrastructure is a Databricks Asset Bundle.
  Functions (schemas) are grouped into *domains*, a business classifier kept in the app's
  registry.
* The UI never contains SQL. Every database interaction goes through a `DatabaseBackend`
  interface with a DuckDB implementation for local development and a Databricks SQL
  implementation for production.
* Three UI tiers per function (Function admin, Editor, Viewer) plus a catalog-level Global
  admin who alone creates and deletes functions, deletes forms and maintains the domain list.

**Non-goals**

* Editing tables with millions of rows. Reference lists are small (hundreds to tens of
  thousands of rows). The grid loads a bounded page (`RDM_MAX_ROWS`, default 5,000) with
  server-side search and sorting.
* Replacing pipelines that build SCD Type 2 dimensions. The app owns the *current state* of a
  list plus its change history; Type 2 dimensions are derived downstream (see §7).

## 2. Repository layout

```
app.py                      Dash entrypoint: gunicorn in production, dev server with --dev
app.yaml                    Databricks Apps runtime config
databricks.yml, resources/  Databricks Asset Bundle (catalog, domain schemas + grants, app)
src/rdm/
  config.py                 Settings from environment (.env for local)
  models.py                 DataType, ColumnDef, FormDef, FunctionDef, DomainDef, User, Role, ChangeSet, ...
  coercion.py               Value coercion shared by grid edits, imports and backends
  backend/base.py           DatabaseBackend abstract interface
  backend/duckdb_backend.py Local backend (DuckDB file, emulates UC metadata in _catalog)
  backend/databricks_backend.py  SQL warehouse backend (databricks-sql-connector)
  backend/sql_utils.py      Identifier quoting, literal escaping, type maps, frame normalisation
  auth/provider.py          MockAuthProvider (personas), DatabricksAuthProvider (headers)
  services/catalog_service.py   Navigation: visible functions/forms, search, grouping by domain
  services/form_service.py      Role guards, positional change-set builder, validation
  services/draft.py             Row-id keyed draft -> ChangeSet; bulk update and restore helpers
  services/files.py             Upload checks and local preview for files
  services/excel_import.py      Excel/CSV parsing, type inference, column sanitising
  ui/                       Dash shell, routing, pages, AG Grid configuration
tests/                      Unit, backend contract and Dash server tests
```

## 3. Domain model

| Concept | Local (DuckDB) | Production (Unity Catalog) |
|---|---|---|
| Catalog | DuckDB database file | `_reference_data` (configurable `RDM_CATALOG`, per bundle target) |
| Domain (classifier) | `_catalog.domains` | `<catalog>._catalog.domains` (registry table, same shape) |
| Function | schema | schema, e.g. `customer__survey_service_improvement` |
| Function's domain | `_catalog.object_properties` (`rdm.domain`) | schema `DBPROPERTIES ('rdm.domain')`, schema tag `rdm_domain`, `_catalog.functions.domain_name` |
| Form | table | Delta table |
| File | `<db dir>/files/<function>/<name>` on disk, read with `read_csv_auto` / `read_parquet` | file in the managed volume `<catalog>.<function>._files`, moved with the Files API, read with `read_files` |
| File attributes | `_catalog.files` | `<catalog>._catalog.files` (same shape) |
| Form description | `COMMENT ON TABLE` | table `COMMENT` |
| Column description | `COMMENT ON COLUMN` | column `COMMENT` |
| Display name, owner, column config, form settings | `_catalog.object_properties` | `TBLPROPERTIES ('rdm.display_name', 'rdm.owner', 'rdm.column_config', 'rdm.settings')`, mirrored to tags `rdm_display_name` / `rdm_owner` (bulk-readable from `information_schema.table_tags`) |
| Function description / owner / doc link | `_catalog.object_properties` (DuckDB cannot comment schemas) | `COMMENT ON SCHEMA` + `DBPROPERTIES` + schema tags |
| Registry | `_catalog.domains` / `functions` / `forms` / `files` | same tables in `<catalog>._catalog` |
| Change history | `_catalog.change_log` | `<catalog>._catalog.change_log` (same shape) + Delta Change Data Feed |
| Shape of the registry and the audit trail | generated from `backend/registry.py` | generated from the same declaration; `tests/test_registry.py` fails if the two dialects drift apart |
| Permissions | `_catalog.grants` (persona groups) | UC schema/catalog privileges via `information_schema` |
| Functions and grants | created/granted in the app | app (`CREATE SCHEMA`, `GRANT`) and bundle; the bundle owns the complete grant list |

### 3.1 Portable data types

| RDM type | DuckDB | Databricks | Grid editor |
|---|---|---|---|
| `STRING` | `VARCHAR` | `STRING` | text, or select when allowed values are defined |
| `INTEGER` | `BIGINT` | `BIGINT` | number (precision 0) |
| `DECIMAL(p,s)` | `DECIMAL(p,s)` | `DECIMAL(p,s)` | number (precision s), formatted |
| `DOUBLE` | `DOUBLE` | `DOUBLE` | number |
| `BOOLEAN` | `BOOLEAN` | `BOOLEAN` | checkbox |
| `DATE` | `DATE` | `DATE` | date picker (ISO string) |
| `TIMESTAMP` | `TIMESTAMP` | `TIMESTAMP` | text, ISO `YYYY-MM-DD HH:MM:SS`, stored as UTC |

Unknown native types (tables created outside the app) are surfaced as `OTHER`, shown
read-only, and never converted. `read_rows` always returns a frame with dtypes fixed by the
RDM type (`normalise_frame`), whatever the driver returned: INTEGER→`Int64`, DECIMAL→`Decimal`
objects, DATE→`date` objects, TIMESTAMP→naive UTC `datetime64[us]`, BOOLEAN→`boolean`.

### 3.2 How the model is allowed to grow

Anything the app knows about an object that SQL cannot express is stored in one of three
places, and each has an additive contract so that the next field is not a migration:

| Where | Holds | Adding to it |
|---|---|---|
| `rdm.column_config` (table property) | per-column rules: allowed values, business keys | a key in `COLUMN_RULE_KEYS`; backends unchanged |
| `rdm.settings` (table property) | per-form options: none yet, by design | a key in `SETTINGS_KEYS`; storage unchanged |
| `_catalog` registry (Delta / DuckDB tables) | what the app records about domains, functions, forms, files and every change | one entry in `backend/registry.py`; both DDLs and both upgrade paths follow |

Both JSON documents carry a version and **preserve keys the reading build does not know**, so
two app versions can share one catalog without either silently stripping the other's data;
registry reads are `SELECT *` and registry writers name their columns, so an added column can
break neither. [DATA_MODEL.md](DATA_MODEL.md) is the full contract and the record of the review
that established it.

### 3.3 System columns (SharePoint-style)

| Column | Type | Purpose |
|---|---|---|
| `_id` | STRING NOT NULL, PK (informational on Databricks) | Stable row identity (UUID) so edits map to `MERGE ... ON _id`. |
| `_version` | BIGINT NOT NULL | Optimistic-concurrency token, incremented on every update; compared as an integer, so it survives every timestamp round trip. |
| `_created_at`, `_created_by` | TIMESTAMP, STRING | Audit |
| `_updated_at`, `_updated_by` | TIMESTAMP, STRING | Audit (display only) |

Business keys (e.g. `cost_centre_code`) are optional column flags used for uniqueness
validation and as the default sort order; `_id` remains the technical key. Business-key
uniqueness is validated in the app (Unity Catalog does not enforce PRIMARY KEY).

## 4. `DatabaseBackend` interface (summary)

```
list_domains / get_domain / create_domain / update_domain / delete_domain   (classifier registry)
list_functions / get_function / create_function / update_function / drop_function   (schemas)
list_forms(function)          light: names, description, display name, owner
get_form(function, name)      full: columns, properties, tags, row count, last change
create_form(FormDef, actor, rows) / update_form_metadata / add_column / drop_column / drop_form
read_rows(form, search, limit, order_by, descending)   normalised frame incl. system columns
count_rows / apply_changes(form, ChangeSet, actor) -> SaveResult / append_rows
get_history(form, limit, row_id=None)                 form-level or per-row history
list_files(function) / get_file / put_file(file, bytes, actor, replace) / update_file_metadata
read_file / preview_file(file, limit) / file_columns / file_history / drop_file
get_permissions(user) -> Permissions / list_function_grants / grant_function_role
```

`delete_domain` refuses while functions are assigned; `drop_function` refuses while the
schema holds tables or files. Both are plain `ConflictError`s the UI shows as they are.
`put_file` counts the rows with the platform reader and rejects unreadable uploads (a
replacement that fails leaves the previous content in place).

Design rules:

* Identifiers are validated with a strict regex at the model layer (leading underscores are
  reserved for system objects) and quoted by the backend; values are always bound as
  parameters. DDL clauses that cannot take parameters (comments, properties, tags) are escaped
  per dialect: `''` doubling for DuckDB, backslash escapes for Databricks (adjacent literals
  are concatenated by Spark, so `''` would silently corrupt text).
* **One save = one atomic write.** DuckDB wraps the change set in a transaction. Databricks
  sends the whole change set as a single JSON parameter to one `MERGE INTO ... USING (SELECT
  inline(from_json(:payload, '<struct schema>')))`: no 255-marker limit, one Delta commit,
  idempotent on retry (row ids are generated once). Updates carry the full row (current values
  merged with the edits) so `UPDATE SET` needs no per-column flags. Bulk loads use the same
  source shape in 500-row chunks.
* Updates and deletes carry the `_version` seen at load time (`WHERE _id = ? AND _version =
  ?` / `ON ... AND t._version = s._version`). A mismatch is reported as a conflict with who
  changed the row and when, never overwritten. Non-conflicting rows in the same save are
  applied.
* Forms are created with `delta.enableChangeDataFeed`, `delta.columnMapping.mode = name`
  (required for `DROP COLUMN`) and `delta.enableDeletionVectors` (row-level concurrency).

## 5. Authentication and authorisation

* `AuthProvider.current_user() -> User(username, display_name, groups)`.
  * Local: `MockAuthProvider` with three personas (header switcher or `RDM_PERSONA`).
  * Databricks: `DatabricksAuthProvider` reads the request headers Databricks Apps injects
    (`X-Forwarded-Email`, `X-Forwarded-Preferred-Username`, `X-Forwarded-User`,
    `X-Forwarded-Access-Token`). Dash callbacks are plain Flask requests, so identity is
    request-scoped; SQL connections are cached per user token (15 min).
* **Recommended production mode: user authorization (on-behalf-of-user) with the `sql`
  scope.** Every statement runs as the signed-in user, so Unity Catalog is the enforcement
  point; the app's role logic only decides what to render. Roles are derived inside the SQL
  session (`information_schema.schema_privileges` / `catalog_privileges` where
  `grantee = current_user() OR is_account_group_member(grantee)`, plus schema ownership), so
  nested account groups resolve without any SCIM call.

| Role | Unity Catalog privileges (schema, plus `USE CATALOG`) |
|---|---|
| Viewer | `USE SCHEMA`, `SELECT`, `READ VOLUME` |
| Editor | Viewer + `MODIFY`, `WRITE VOLUME` |
| Function admin | Editor + `CREATE TABLE`, `CREATE VOLUME`, `MANAGE`, `APPLY TAG` (or schema ownership) |
| Global admin | `CREATE SCHEMA` / `MANAGE` on the catalog, or catalog ownership |

  `MANAGE` on the schema is inherited by every table and volume, so function admins can
  alter forms whoever created them; the *app* nevertheless reserves `drop_form`, `drop_file`
  and `drop_function` to global admins (`FormService.require_global_admin`), which is a
  workflow rule on top of the UC privileges. File operations go through the Files API with
  the user's token (user authorization scope `files.files`), so `READ VOLUME` / `WRITE
  VOLUME` decide who can download and replace. UC `MODIFY` also permits column DDL outside the app; the app is the workflow,
  `DESCRIBE HISTORY` is the audit of such changes.
* Functions and grants can be created from the app (`CREATE SCHEMA`, `GRANT`/`REVOKE` run as
  the signed-in user) and are also declared in `resources/schemas.yml`. The bundle manages the
  complete grant list of a schema, so grants made in the app must be copied into the YAML to
  survive the next deploy.
* Only global admins assign functions to domains; a function admin editing function details
  cannot change the domain (checked in `FormService.update_function`).
* Service-principal fallback (no OBO): roles are computed from the principals resolved by the
  provider and the service principal must hold the union of privileges - documented, not
  recommended.

## 6. Grid editing model (Dash + AG Grid)

1. `read_rows` → JSON records; the grid uses `getRowId = _id`, `_id`/`_version` are data
   fields without columns (nothing to reveal or edit).
2. Edits arrive as `cellValueChanged` events (row id, column, new value, row data with
   `_version`) and are folded into a browser-side `Draft` store keyed by `_id`
   (`updates`, `inserts` with temporary ids, `deletes`). Reloading rows (search, sort,
   refresh) re-applies the draft as an overlay, so searching while editing is safe.
3. `build_changeset_from_draft` validates on every edit (coercion, required, allowed
   values, unique business keys); issues are listed and the offending cells are highlighted
   through per-row flags. Save is disabled while issues exist.
4. Save → `FormService.save` → `apply_changes`; the result is shown as a notification and
   conflicts as an alert; the grid reloads.
5. Viewers get the same grid read-only; editor controls are rendered hidden for them (Dash
   callbacks need their components present) and the service layer enforces the role again.

6. **Bulk update** (FR-22): the selected rows and one column/value pair go through
   `bulk_value` (coercion, required, allowed values) and `Draft.set_many`; the grid is patched
   with a row transaction. **Item form** (FR-24): one row's fields in a modal, each rendered
   by `_value_control` for the column type; applying compares field values with the row and
   folds the differences into the draft. **Restore**: `restore_row` stages a history snapshot
   on the current row (or as a new row when the row no longer exists); the item form offers
   it per version, the History tab for the selected entry including deletions.
7. The form's tabs are kept mounted (`dmc.Tabs(keepMounted=True)`) so the grid, its
   transactions, the pending bar and the Save state survive a round trip to History /
   Schema / Settings; returning to the Data tab additionally recomputes the Save state from
   the draft (`sync_save_state`).

Multi-cell clipboard paste is an AG Grid Enterprise feature; bulk update and the Excel/CSV
import (append) cover bulk changes instead.

## 7. History and slowly changing dimensions

* The grid is the **current state** (SCD Type 1 semantics) of a reference list.
* Every save writes row-level entries (before/after JSON, actor, batch) to
  `_catalog.change_log` on both backends, so the History tab is identical locally and in
  production and does not depend on Delta log retention. `get_history(row_id=...)` reads the
  same table for one row (item form, restore). Delta Change Data Feed stays enabled on every
  form for downstream pipelines and is the fallback if the audit table is missing.
* The audit entry says what kind of object changed (`object_type`) and carries a free-form
  `meta_json` envelope, so recording a definition change, a grant or an approval later needs
  new values rather than a new shape. `before_json` / `after_json` stay opaque snapshots -
  that is what lets the trail outlive any change to the form it describes.
* The optional per-form Type 2 table (FR-47) is maintained whenever it **exists**, not only
  while the flag is on, because it outlives `set_scd2(False)`; and disabling closes every open
  window, because `__END_AT IS NULL` is published to consumers as "this is the current
  version" and must not go on advertising a value the form no longer holds.
* Type 2 dimensions should be built downstream from CDF (Lakeflow Declarative Pipelines
  `AUTO CDC ... STORED AS SCD TYPE 2`), not maintained by hand in the grid.
* The app does not add business validity dates: it tracks *who changed what and when* with
  its own audit columns and history. Lists that need validity periods define them as
  ordinary columns.

## 8. Administrator features

* **Form creator**: upload → sheet/header choice → inferred columns editable in a grid
  (name, type, description, required, business key, allowed values suggested for
  low-cardinality text) → function/name/description/owner → review with coercion issues →
  create (+ system columns, comments, properties, tags) and load the file's rows in the
  same step (forms started from scratch begin empty).
* **Schema editor**: descriptions, required, business key, allowed values; add and remove
  columns. Types are fixed after creation (a type change is a rewrite).
* **Settings**: display name, description, owner; delete form with typed confirmation
  (global admins only).
* **Functions**: overview, details incl. domain (global admins), grants, delete when empty
  (global admins only).
* **Domains** (global admins): list, create, edit, delete when unassigned. The sidebar and
  the home page group functions under their domain; unassigned functions form a trailing
  group.
* **Files**: *New file* on the function page or in the sidebar (function admins) uploads a CSV/Parquet file
  through `dcc.Upload` (limit `RDM_MAX_FILE_MB`), previews the first rows locally
  (`services/files.preview_bytes`) and stores it with `put_file`; the file page previews the
  first rows through the backend reader, lists inferred columns and the upload history,
  downloads (`dcc.send_bytes`), replaces (editors) and deletes (global admins). Files found
  in the volume without a registry entry are listed as unregistered.

## 9. Local development and testing strategy

DuckDB is the local *runtime* backend; it is deliberately not the only truth:

| Layer | What | Runs where |
|---|---|---|
| Unit | models, coercion, draft/change-set building, validation, type inference, identifier rules, Databricks SQL generation against a fake connection | every commit, milliseconds |
| Contract | `DatabaseBackend` behaviour against DuckDB (create/alter, read, save, conflicts, history, permissions) | every commit |
| Server | Dash app boots, layout and callbacks resolve (Flask test client) | every commit |
| Browser | Playwright: edit/add/delete/save, bulk update, item form + restore, tab round trip, function and domains pages, persona switch | on demand (`scripts` in the session; add to CI when a browser is available) |
| Real | `databricks apps run-local` against a dev workspace; the live contract suite (`tests/test_databricks_live.py`) against a dev catalog with `RDM_TEST_DATABRICKS=1` | before release |

Known DuckDB/Databricks divergences (covered by the Databricks SQL-generation tests and the
live contract suite rather than by DuckDB): PRIMARY KEY enforcement, literal escaping,
DROP COLUMN requirements, MERGE metrics, tags/properties, CDF retention. Timestamp
semantics are pinned: the SQL connection sets the session timezone to UTC, so naive
timestamps round-trip unchanged regardless of the warehouse's default zone.

## 10. Databricks Apps practices applied

* `app.yaml` at repo root runs `python app.py`, which starts gunicorn on
  `DATABRICKS_APP_PORT`; the SQL warehouse is injected with `valueFrom: sql-warehouse`.
* Request-scoped identity from the forwarded headers; no secrets in code; least privilege for
  the app service principal (`CAN_USE` on the warehouse; data access through user
  authorization).
* Native parameter binding, identifier validation, per-user connection cache, short
  navigation/permission caches (`RDM_METADATA_CACHE_TTL`).
* Catalog confinement and no-cascade deletes enforced in the backend itself (§11).
* Serverless SQL warehouse recommended (cold-start latency dominates UX otherwise).
* Everything (catalog, schemas, grants, app) is declared in the bundle.

## 11. Security review: deletions and catalog confinement

Reviewed with the question "can a user delete something they should not, or reach anything
outside `_reference_data`?". The app never holds broader rights than the signed-in user
(user authorization), so Unity Catalog remains the ultimate boundary; the controls below are
what the *app* guarantees on top of it, and what was hardened.

### 11.1 Who can delete what

| Action | Guard in `FormService` | UI confirmation | Backend statement |
|---|---|---|---|
| Delete rows of a form | `require(function, EDITOR)` | part of Save; deleted rows stay in History and can be restored | `MERGE ... WHEN MATCHED AND op = 'D' THEN DELETE` on that table only |
| Remove a column | `require(function, ADMIN)` | typed column name | `ALTER TABLE ... DROP COLUMN` |
| Delete a form | `require_global_admin` | typed form name | `DROP TABLE` (Delta history keeps it recoverable) |
| Delete a file | `require_global_admin` | typed file name | Files API `delete` of that one path |
| Delete a function | `require_global_admin` | typed function name | `DROP VOLUME IF EXISTS` then `DROP SCHEMA ... RESTRICT`, only when empty |
| Delete a domain | `require_global_admin` | typed domain name | registry `DELETE`, only when no function is assigned |
| Revoke a group's role | `require(function, ADMIN)` | explicit button | `REVOKE` of the app-managed privileges on that schema |

Every guard is server-side (Dash callbacks are Flask requests; hiding a button is never the
control). Global admin is derived from catalog-level privileges (`CREATE SCHEMA` / `MANAGE`
on the catalog or ownership), never from a request header or a browser value. The local
persona switcher exists only in the mock provider (`RDM_AUTH=mock`); the Databricks provider
ignores it.

### 11.2 Confinement to the catalog

* Every identifier that reaches SQL (catalog, function, form, column, file name) is validated
  against `^[a-z_][a-z0-9_]{0,62}$` and back-quoted; user-created names cannot start with
  `_`, so `_catalog` and `_files` cannot be targeted through a name. Values are bound as
  parameters; the few DDL literals (comments, properties, tags) are escaped with Spark rules.
* `DatabricksBackend._assert_confined` runs on **every** statement before it is sent: any
  three-part name, `ON CATALOG` clause, `table_changes('...')` argument or `/Volumes/...`
  literal must name `self.catalog`, and `CASCADE`, `DROP CATALOG`, `TRUNCATE`, `PURGE`,
  `VACUUM` and a bare `USE CATALOG` are refused. String literals are blanked before the
  clause check so a description mentioning "cascade" is not affected. This is defence in
  depth against a future coding mistake; the app's own statements never trigger it.
* `DatabricksBackend._assert_volume_path` accepts only
  `/Volumes/<catalog>/<function>/_files[/<file>]`, so the Files API can list, read, write
  and delete nothing else (no `..`, no sub-folders, no other volume).
* The bundle grants the app service principal `CAN_USE` on the warehouse only; data access is
  the user's. In the (documented, not recommended) service-principal mode the app's guards
  are the only enforcement point, which is why the guards are in the service layer and not
  in the UI.

### 11.3 Findings and fixes

| # | Finding | Fix |
|---|---|---|
| 1 | `drop_function` counted only CSV/Parquet files before `DROP VOLUME`; other files or sub-folders in the volume would have been deleted with it. | Any entry in the volume blocks the deletion ("still has N file(s) or folder(s)"). DuckDB: same rule for the local folder, which is now removed with a non-recursive `rmdir`. |
| 2 | A volume that could not be listed (no `READ VOLUME`, Files API error) was treated as empty. | Listing errors other than *not found* abort the deletion ("Cannot verify that the file volume ... is empty"). |
| 3 | A new file was uploaded with `overwrite=True`; if the existence check failed transiently, an existing file could have been replaced without the *Replace* flow (and without its history entry). | New files upload with `overwrite=False` (the API's *already exists* error becomes a conflict); only *Replace* overwrites. |
| 4 | `DROP SCHEMA` relied on the default (no cascade). | Explicit `RESTRICT`, and `CASCADE` is refused by the confinement guard. |
| 5 | Nothing prevented a future statement from naming another catalog. | `_assert_confined` / `_assert_volume_path` (above), covered by tests. |

Observations that were reviewed and kept as designed: function admins can revoke any group's
role on their own function (a global admin's catalog-level rights are not affected, so there
is no lockout); granting a role also grants `USE CATALOG` best-effort; `drop_column` is a
function-admin action with a typed confirmation because it is a definition change, not a
deletion of an object.
