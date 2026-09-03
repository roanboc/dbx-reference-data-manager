# Deploying the Reference Data Manager as a Databricks App

This guide takes the app from the local DuckDB sandbox to a Databricks App managed by a
Databricks Asset Bundle (DAB). Everything the app needs in a workspace - the Unity Catalog
catalog, the function schemas with their grants, the SQL-warehouse binding and the app itself
- is declared in `databricks.yml` and `resources/*.yml`.

> Status: this scaffolding has been checked for YAML syntax and internal consistency only.
> Run `databricks bundle validate` against your own workspace before the first deploy and
> fix whatever it reports (host, groups, warehouse, CLI version differences).

| File | Purpose |
|---|---|
| `app.yaml` | Databricks Apps runtime config (command + env). Used by `databricks apps deploy` and `databricks apps run-local`. |
| `databricks.yml` | Bundle name, variables (`catalog`, `warehouse_id`, `admin_group`, `app_name`, `app_users_group`), targets `dev` and `prod`. |
| `resources/catalog.yml` | The `_reference_data` catalog and its catalog-level grants. |
| `resources/schemas.yml` | One schema per business function (with its domain as a property), with Viewer / Editor / Function admin grants. |
| `resources/app.yml` | The app: source path, env, warehouse binding, user-authorization scopes, who may open it. |
| `.github/workflows/ci.yml` | Lint + tests on every push/PR; `bundle validate` + `deploy -t prod` from `main`. |

How the pieces fit at runtime:

```
browser --> Databricks Apps proxy --> python app.py (gunicorn, listens on DATABRICKS_APP_PORT)
             adds X-Forwarded-Email / -Preferred-Username
             adds X-Forwarded-Access-Token   (user authorization, scope `sql`)
                                                  |
                       DatabricksAuthProvider  <--+  (RDM_AUTH=databricks)
                       DatabricksBackend  --token-->  SQL warehouse (DATABRICKS_WAREHOUSE_ID)
                                                        runs AS THE USER
                                                        Unity Catalog enforces schema grants
```

## Roles at a glance

| Role | Scope | Unity Catalog privileges | Typical group |
|---|---|---|---|
| Viewer | one function (schema) | `USE SCHEMA`, `SELECT`, `READ VOLUME` | `<function>_readers` |
| Editor | one function | Viewer + `MODIFY`, `WRITE VOLUME` | `<function>_stewards` |
| Function admin | one function | Editor + `CREATE TABLE`, `CREATE VOLUME`, `MANAGE`, `APPLY TAG` | `<function>_admins` |
| Global admin | the catalog | `USE CATALOG`, `CREATE SCHEMA`, `MANAGE` (or catalog owner) | data platform / data engineering |

The hierarchy is domain > function > form | file: a *function* is a schema, a *domain* is a
business classifier (the registry table `_catalog.domains`, maintained by global admins in
the app) recorded on each schema as the property `rdm.domain` and the tag `rdm_domain`, a
*file* is a CSV/Parquet file in the managed volume `_files` the app creates in the function
schema (needs `CREATE VOLUME` for the creator, `READ VOLUME` / `WRITE VOLUME` for users). All groups
also need `USE CATALOG` on the catalog. Grants can be made from the app's function page
(groups only; the app runs `GRANT`/`REVOKE` as the signed-in user) or declared in the
bundle. Only global admins delete functions and forms. The app keeps the registry of
domains, functions and forms and the audit trail in the `_catalog` schema
(`resources/schemas.yml`).

## 1. Prerequisites

Workspace

* Unity Catalog enabled; the deploying identity may `CREATE CATALOG` on the metastore
  (or an admin pre-creates the catalog and you adopt it, see §9).
* A SQL warehouse, ideally serverless (cold starts dominate the UX otherwise). Note its id
  (`databricks warehouses list`). The deploying identity needs CAN_MANAGE on it so the bundle
  can grant the app's service principal CAN_USE.
* Databricks Apps available in the workspace, and the *user authorization* feature for apps
  enabled (if the app's Authorization tab does not offer it, a workspace admin has to enable
  the preview first).
* Account-level groups for the roles. Unity Catalog grants only work with account groups,
  not workspace-local ones. The defaults used by the bundle and the local demo are
  `rdm_admins`, `student_readers`, `student_stewards`, `finance_admins`, `finance_readers`,
  `finance_stewards`, `hr_readers`, `hr_stewards`; rename them in `resources/schemas.yml`
  to match your directory.

Your machine

* Databricks CLI >= 0.2xx with bundle support (`databricks --version`; install per the
  official instructions, not the PyPI `databricks-cli` package).
* `databricks auth login --host https://<workspace>` (creates a profile; the bundle uses it).
* Python 3.11 and the dev requirements if you want to run tests: `make install`.

## 2. Configure the bundle

1. `databricks.yml` - set `workspace.host` for `dev` and `prod`; for `prod` set
   `run_as.service_principal_name` to the application ID of the service principal that CI
   will authenticate as. **The identity that deploys and `run_as` must be the same** - the
   CLI refuses to deploy apps with a different `run_as`.
2. Variables (`databricks.yml` -> `variables`):

   | Variable | Default | Notes |
   |---|---|---|
   | `catalog` | `_reference_data` (`_reference_data_dev` in `dev`) | Created by the bundle. Must not already exist unless you bind it (§9). |
   | `warehouse_id` | none | Required. `--var="warehouse_id=<id>"`, `BUNDLE_VAR_warehouse_id=<id>`, or a per-target value in `databricks.yml`. |
   | `admin_group` | `rdm_admins` | Catalog administrators; also CAN_MANAGE on the app. |
   | `app_name` | `reference-data-manager` (`-dev` in `dev`) | Lowercase, digits, hyphens; unique per workspace. |
   | `app_users_group` | `users` | Who may open the app. |

3. `resources/schemas.yml` - one block per function. Copy an existing block to add a
   function (name `<domain>__<area>`, lowercase, no leading underscore), set `comment`,
   `rdm.display_name`, `rdm.owner`, `rdm.domain` (a name from the app's domain list) and the
   reader/steward/admin groups. Domains themselves are created in the app (**Domains**, global
   admins) - the bundle only records the assignment on the schema.
4. `app.yaml` - only needed when deploying **without** the bundle or for `run-local`; keep
   `RDM_CATALOG` in sync with the `catalog` variable.
   Optional: `RDM_ADMIN_CONTACT` (an e-mail address, URL or text) is shown in **Help > About** as
   the person or team to contact for use cases the app is not designed for.

## 3. Deploy to `dev`

```bash
export BUNDLE_VAR_warehouse_id=<warehouse id>       # or --var="warehouse_id=..."

databricks bundle validate -t dev                   # resolves variables, checks the config
databricks bundle deploy   -t dev                   # catalog, schemas, grants, app + source upload
databricks bundle run reference_data_manager -t dev # starts the app (deploys the uploaded source)
databricks bundle summary  -t dev                   # prints resource names and the app URL
databricks bundle open reference_data_manager -t dev
```

What `dev` (mode `development`) does differently:

* Schema names are prefixed with `dev_<your short name>_`, so the sidebar shows functions
  such as `dev_alice_student__survey_service_improvement`. Catalog and app names are not
  prefixed; the target therefore overrides them (`_reference_data_dev`, `reference-data-manager-dev`).
  Several developers sharing one workspace should pass distinct `--var="catalog=..."` and
  `--var="app_name=..."` values, because each developer keeps a separate bundle state.
* The deployment runs as you, so you own the catalog and schemas; no `run_as`.

Iterating: after changing code run `databricks bundle deploy -t dev` followed by
`databricks bundle run reference_data_manager -t dev`. Application logs are at
`<app URL>/logz`. `databricks bundle destroy -t dev` removes the app, the schemas and the
catalog - including their data.

## 4. Enable user authorization (on-behalf-of-user) and why

Recommended production mode (DESIGN.md §5). Without it the app talks to the warehouse as
its own service principal, which would then need the union of every user's data privileges
and become the security boundary. With it, the proxy forwards a short-lived user token in
`X-Forwarded-Access-Token`; `DatabricksBackend` opens the SQL connection with that token, so
every statement runs as the signed-in user and Unity Catalog enforces the schema grants.
The app's role logic then only decides what to render.

* Bundle deployments: already configured - `resources/app.yml` declares
  `user_api_scopes: [sql, files.files, iam.current-user:read]`. `sql` allows warehouse
  queries under the user's Unity Catalog permissions; `files.files` lets the app upload,
  download and delete files in the function volumes as the user (Files API);
  `iam.current-user:read` lets `DatabricksAuthProvider` read the user's group memberships
  for the sidebar (a UI default scope, declared explicitly).
* UI: Compute -> Apps -> *your app* -> **Authorization** -> enable *User authorization*,
  tick the `sql` scope, save. The app restarts.
* CLI (apps deployed without the bundle):
  `databricks apps update <app-name> --json '{"user_api_scopes": ["sql", "files.files", "iam.current-user:read"]}'`
* Users see a consent screen listing the scopes on their first visit. Until they accept, no
  user token is forwarded and the app cannot run queries on their behalf.
* Each user also needs **CAN USE on the SQL warehouse** - the query runs as them. Grant it to
  the app users group in the warehouse's Permissions dialog or with
  `databricks permissions update warehouses <id> --json '{"access_control_list":[{"group_name":"users","permission_level":"CAN_USE"}]}'`.

## 5. Grant access to groups

The bundle is the single place where privileges are declared (DESIGN.md §5):

| Role in the app | Privileges | Where |
|---|---|---|
| Viewer | `USE_CATALOG` on the catalog; `USE_SCHEMA`, `SELECT`, `READ_VOLUME` on the function schema | `catalog.yml` (`account users`) + `schemas.yml` |
| Editor | Viewer + `MODIFY`, `WRITE_VOLUME` | `schemas.yml` |
| Function admin | Editor + `CREATE_TABLE`, `CREATE_VOLUME`, `MANAGE`, `APPLY_TAG` | `schemas.yml` (inherited from `catalog.yml` for `admin_group`) |
| Global admin (creates and deletes functions, deletes forms, maintains domains) | `CREATE_SCHEMA` (+ the above) on the catalog | `catalog.yml` |

Rules of thumb

* Grants go to account groups. Add a user to a group in the account console; the app picks
  it up after its group cache expires (5 minutes) or a page reload with a new session.
* The bundle manages the **complete** grant list of the catalog and of each schema.
  Anything granted outside it - Catalog Explorer, `GRANT` in SQL, the app's own grant
  action - is reverted on the next deploy. Put every group in the YAML.
* Opening the app is a separate permission (`resources/app.yml` -> `permissions`); the
  default gives `users` CAN_USE and `admin_group` CAN_MANAGE.
* `USE_CATALOG` for `account users` is granted in `catalog.yml` so that function groups only
  need schema-level grants. It reveals only the catalog name. Replace it with per-group
  entries if your policy does not allow it.

## 6. Run locally against the workspace

Use this to test the real backend with the real identity headers before a release
(`databricks apps run-local` emulates the Apps proxy and injects the `X-Forwarded-*`
headers for the CLI's signed-in user).

`.env` (never committed):

```dotenv
RDM_BACKEND=databricks
RDM_AUTH=databricks
RDM_CATALOG=_reference_data_dev
DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
DATABRICKS_WAREHOUSE_ID=<warehouse id>
# RDM_MAX_ROWS=5000
```

Then, with a CLI profile logged in to the same workspace:

```bash
databricks apps run-local --prepare-environment   # creates a venv from requirements.txt on first run
# see `databricks apps run-local --help` for --port / --debug / --entry-point
```

`app.yaml` is read for the command and env. `valueFrom: sql-warehouse` can only be resolved
when the app exists in the workspace with that resource; the `.env` value fills the gap
because `Settings.from_env` loads `.env` without overriding variables that are already set.
If the CLI exports an empty `DATABRICKS_WAREHOUSE_ID`, export the real value in your shell
before starting.

Plain `python app.py --dev` with `RDM_AUTH=databricks` fails with "No user identity
headers found" - that is expected: use `run-local`, or `RDM_AUTH=mock` with the DuckDB
backend for UI work.

## 7. Run the contract tests against a dev catalog

The contract suite (`tests/`, DESIGN.md §9) runs against DuckDB always and against the
Databricks backend when `RDM_TEST_DATABRICKS=1`. Point it at a **dev** catalog only - the
tests create and drop schemas and tables.

```bash
export RDM_TEST_DATABRICKS=1
export RDM_BACKEND=databricks
export RDM_CATALOG=_reference_data_dev                  # never the production catalog
export DATABRICKS_HOST=https://<workspace>.cloud.databricks.com
export DATABRICKS_WAREHOUSE_ID=<warehouse id>
export DATABRICKS_TOKEN=<token>               # or a CLI profile the SDK can resolve
python -m pytest tests -k databricks
```

The identity running the tests needs the Administrator set on the catalog (it is the
`admin_group` member in `dev`, i.e. you). In CI this can be a job in the workspace or a
second workflow job with the same secrets; it is intentionally not part of `ci.yml`.

## 8. Deploy to `prod` (CI)

`.github/workflows/ci.yml` runs `databricks bundle validate -t prod` and
`databricks bundle deploy -t prod` (then `bundle run` to start the app) on `workflow_dispatch`
and on every push to `main`, after lint and tests pass. Configure in the `prod` GitHub
environment:

* secrets `DATABRICKS_HOST`, `DATABRICKS_TOKEN` - token of **the service principal named in
  `targets.prod.run_as`** (apps must be deployed by that identity). Prefer GitHub OIDC
  federation (`DATABRICKS_AUTH_TYPE=github-oidc` + `DATABRICKS_CLIENT_ID`, commented in the
  workflow) over a stored token.
* variable `DATABRICKS_WAREHOUSE_ID` - mapped to `BUNDLE_VAR_warehouse_id`.

The service principal needs: CREATE CATALOG on the metastore (first deploy), CAN_MANAGE on
the warehouse, permission to create apps, and the `prod` `root_path`
(`/Shared/.bundle/prod/rdm_reference_data_manager`) must be writable by it. It becomes the
owner of the catalog and the schemas; the `admin_group` gets MANAGE on both so
administrators can still alter and drop tables that the app or other users created.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no value assigned to required variable warehouse_id` | Pass `--var="warehouse_id=..."`, export `BUNDLE_VAR_warehouse_id`, or set it in the target. |
| `apps do not support a setting a run_as user that is different from the owner` | `prod` is being deployed with credentials of a different identity than `run_as`. Use the service principal's own credentials (or remove `run_as` in a personal target). |
| Deploy fails: catalog already exists | The bundle creates the catalog and cannot adopt an existing one silently. Choose another `catalog` value, or bind the existing one: `databricks bundle deployment bind forms <catalog name> -t <target>` (see `databricks bundle deployment bind --help`). |
| App starts but every query fails with a permission error on the warehouse | The **user** (with user authorization) or the app's service principal (without) lacks CAN USE on the warehouse - §4. The bundle only grants the service principal. |
| `X-Forwarded-Access-Token` missing / app runs as the service principal / "insufficient scope" | User authorization not enabled, the `sql` scope not declared, or the user has not consented yet. Check the app's Authorization tab; users must reload and accept the consent screen. Scopes added later require re-consent. |
| `No user identity headers found` | `RDM_AUTH=databricks` outside the Apps proxy. Use `databricks apps run-local` or `RDM_AUTH=mock` locally. |
| Sidebar shows no functions for a user who "should" have access | The user's group has no `USE_SCHEMA`+`SELECT` on the schema, the group is workspace-local instead of account-level, or the grant was added outside the bundle and reverted by a deploy. Fix `resources/schemas.yml` and redeploy. |
| Group memberships missing in the sidebar (roles look wrong) | `iam.current-user:read` scope missing (user authorization) or, in service-principal mode, the service principal cannot read users. Roles are still enforced by Unity Catalog; only rendering is affected. |
| Function names look odd in `dev` (`dev_alice_...`) | Development-mode prefix on schemas; expected. `prod` uses the plain names. |
| Uploading a file fails with a permission error, or files are listed without sizes | The user lacks `WRITE VOLUME` (upload/replace/delete) or `READ VOLUME` (list/download) on the function schema, the `files.files` scope is not declared, or the `_files` volume could not be created (`CREATE VOLUME`). Files landed outside the app are listed but unregistered until an admin describes them. |
| A function shows under "Unassigned" | Its schema has no `rdm.domain` property / `rdm_domain` tag (created by the bundle without it, or outside the app). A global admin assigns the domain on the function page. |
| Catalog name `_reference_data` (leading underscore) | Valid Unity Catalog name and a valid unquoted identifier in Databricks SQL; the backend quotes every identifier with backticks anyway. Some organisations reserve leading underscores for system objects in their naming policy - check yours, and note the app itself rejects leading underscores for *function* and *domain* names. |
| App status `UNAVAILABLE` / crash loop after deploy | Open `<app URL>/logz`. Usual causes: dependency pin in `requirements.txt` incompatible with the Apps Python runtime, or a `PORT`/`DATABRICKS_APP_PORT` override in `app.yaml`/`config` (the runtime sets the port; `app.py` reads it). |
| `DATABRICKS_WAREHOUSE_ID (or DATABRICKS_HTTP_PATH) must be set` | The `sql-warehouse` app resource is missing or its key differs from `valueFrom`/`value_from`. Check `resources/app.yml` (bundle) or the app's Resources tab (manual deploy). |
