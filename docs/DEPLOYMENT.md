# Deploying the Reference Data Manager as a Databricks App

This guide takes the app from the local DuckDB sandbox to a Databricks App managed by a
Databricks Asset Bundle (DAB). Everything the app needs in a workspace - the Unity Catalog
catalog, the domain schemas with their grants, the SQL-warehouse binding and the app itself
- is declared in `databricks.yml` and `resources/*.yml`.

> Fill in the bundle variables for your own workspace before the first deploy: every value
> that depends on an environment (`warehouse_id`, `storage_root`, `admin_group`,
> `breakglass_group`, the workspace host) ships as an obvious placeholder. Run
> `databricks bundle validate` after every configuration change.

## Where the app runs

The app needs one workspace: it hosts the Databricks App and the SQL warehouse it queries.
Any other workspace on the same metastore can consume the reference data in its pipelines,
read-only, without the app being deployed there.

Two workspace-level choices are worth making deliberately:

* **Which workspace hosts the app.** Users open the app there, so it is normally the
  workspace they already sign in to, not a data-engineering workspace.
* **Whether the catalog is visible everywhere.** By default a catalog is visible to every
  workspace attached to the metastore. Restrict it if you want the app's workspace to be
  the only one that can write.

### Restricting the catalog to its workspaces

The bundle schema has no isolation field, so isolation and the bindings are set with two
CLI calls after the **first** deploy, run as the catalog owner or a metastore admin; both
persist over redeploys:

```bash
# 1. Isolate the catalog; this auto-binds the workspace the call goes through (read-write),
#    so run it against the workspace that hosts the app.
databricks catalogs update <catalog name> --isolation-mode ISOLATED
# 2. Add each consuming workspace read-only.
databricks workspace-bindings update-bindings catalog <catalog name> --json \
  '{"add":[{"workspace_id":<consuming workspace id>,"binding_type":"BINDING_TYPE_READ_ONLY"}]}'
databricks workspace-bindings get-bindings catalog <catalog name>   # verify
```

A read-only binding blocks writes and DDL from that workspace, so pipelines there can read
every form but never modify one; the app and the bundle manage the catalog only through the
workspace that hosts the app.

| File | Purpose |
|---|---|
| `app.yaml` | Databricks Apps runtime config (command + env). Used by `databricks apps deploy` and `databricks apps run-local`. |
| `databricks.yml` | Bundle name, variables (`catalog`, `warehouse_id`, `storage_root`, `admin_group`, `breakglass_group`, `app_name`, `app_users_group`, `auth_mode`, `debug_personas`) and the example `dev` target. |
| `resources/catalog.yml` | The reference-data catalog and its catalog-level grants. |
| `resources/schemas.yml` | One schema per business function (with its domain as a property), with Viewer / Editor / Function admin grants. |
| `resources/app.yml` | The app: source path, env, warehouse binding, user-authorization scopes, who may open it. |
| `.github/workflows/ci.yml` | Lint, tests and the publish-safety scan on every push/PR. Deployment is not automated: wire it up in your fork with your own credentials. |

How the pieces fit at runtime:

```
browser --> Databricks Apps proxy --> python app.py (gunicorn, listens on DATABRICKS_APP_PORT)
             adds X-Forwarded-Email / -Preferred-Username / -User
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

* Unity Catalog enabled; the deploying identity may `CREATE CATALOG` on the metastore.
  On the frontend workspaces `CREATE CATALOG` is typically not granted: pre-create the
  catalog from a workspace that does allow it, or as a metastore admin, then adopt it with
  `databricks bundle deployment bind forms <catalog>` before the first deploy (see §9).
* A SQL warehouse, ideally serverless (cold starts dominate the UX otherwise). Note its id
  (`databricks warehouses list`). The deploying identity needs CAN_MANAGE on it: attaching
  the warehouse as an app resource requires it, and the attachment is what grants the app's
  service principal CAN_USE. On locked-down workspaces a workspace admin grants CAN_MANAGE
  to the deploying identity once.
* Databricks Apps available in the workspace, and the *user authorization* feature for apps
  enabled (if the app's Authorization tab does not offer it, a workspace admin has to enable
  the preview first).
* Account-level groups for the roles. Unity Catalog grants only work with account groups,
  not workspace-local ones. Set `admin_group` (global administrators) and
  `breakglass_group` (a second group with the same privileges, so the catalog is never left
  without an administrator) to groups that exist in your account; the local demo uses mock
  groups (`rdm_admins`, `finance_stewards`, ...) that exist only in the DuckDB backend.
  Add function groups in `resources/schemas.yml`.

Your machine

* Databricks CLI >= 0.2xx with bundle support (`databricks --version`; install per the
  official instructions, not the PyPI `databricks-cli` package).
* `databricks auth login --host https://<workspace>` (creates a profile; the bundle uses it).
* Python 3.11 and the dev requirements if you want to run tests: `make install`.

## 2. Configure the bundle

1. `databricks.yml` - set `workspace.host` on the `dev` target to your workspace. If you
   deploy from CI with a service principal, name it in `run_as.service_principal_name`:
   **the identity that deploys and `run_as` must be the same** - the CLI refuses to deploy
   apps with a different `run_as`.
2. Variables (`databricks.yml` -> `variables`). Each one ships with a placeholder default;
   replace them all before deploying:

   | Variable | Value | Notes |
   |---|---|---|
   | `catalog` | `_reference_data` | Created by the bundle. Must not already exist unless you bind it (§9). Give each environment its own name. |
   | `warehouse_id` | your SQL warehouse | Or `--var="warehouse_id=<id>"` / `BUNDLE_VAR_warehouse_id`. Serverless recommended. |
   | `storage_root` | your managed storage location | Only needed when the metastore has no default storage root; otherwise delete the line from `resources/catalog.yml`. |
   | `admin_group` | an account group | Global administrators; also CAN_MANAGE on the app. |
   | `breakglass_group` | an account group | Same privileges as `admin_group` everywhere. Use the same value if you run only one. |
   | `app_name` | `reference-data-manager` | Lowercase, digits, hyphens; unique per workspace. |
   | `app_users_group` | `users` | Who may open the app. |
   | `auth_mode` / `debug_personas` | `databricks` / `"false"` | Change only on a deployment holding test data (see below). |

3. `resources/schemas.yml` - one block per function. Copy an existing block to add a
   function (name `<domain>__<area>`, lowercase, no leading underscore), set `comment`,
   `rdm.display_name`, `rdm.owner`, `rdm.domain` (a name from the app's domain list) and the
   reader/steward/admin groups. Domains themselves are created in the app (**Domains**, global
   admins) - the bundle only records the assignment on the schema.
4. `app.yaml` - only needed when deploying **without** the bundle or for `run-local`; keep
   `RDM_CATALOG` in sync with the `catalog` variable.
   Optional: `RDM_ADMIN_CONTACT` (an e-mail address, URL or text) is shown in **Help > About** as
   the person or team to contact for use cases the app is not designed for.

## 3. Deploy

```bash
databricks bundle validate -t dev --profile <your profile>   # resolves variables, checks the config
databricks bundle deploy   -t dev --profile <your profile>   # catalog, schemas, grants, app + source upload
databricks bundle run reference_data_manager -t dev --profile <your profile>   # starts the app
databricks bundle summary  -t dev --profile <your profile>   # prints resource names and the app URL
databricks bundle open reference_data_manager -t dev --profile <your profile>
```

Notes on the target:

* It deliberately does **not** use `mode: development`: the development-mode schema prefix
  (`dev_<name>_`) would rename the registry schema, which the app resolves by the fixed
  name `_catalog`, and hang every grant on the wrong schemas. Give each environment its own
  catalog name instead.
* One person deploys a target at a time; a second person needs distinct
  `--var="catalog=..."` and `--var="app_name=..."` values, because each keeps a separate
  bundle state.
* The deployment runs as you, so you own the catalog and schemas; no `run_as`.
* **Review personas** are optional: setting `auth_mode: mock` and `debug_personas: "true"`
  lets anyone who can open the app switch between Global admin, Function admin, Editor and
  Viewer in the header and experience each role on every function. Queries then run as the
  app's service principal, so keep it to a catalog with test data; production always uses
  `auth_mode: databricks`. In that mode the app's service principal needs the admin
  privilege set on the catalog - add its client id to the catalog grant list (recreating
  the app mints a new one, so update it then).
* After the **first** deploy, isolate the catalog and add the read-only binding of the
  data platform workspace ("Workspace topology" above); both are kept over redeploys.

Iterating: after changing code run `databricks bundle deploy -t developer` followed by
`databricks bundle run reference_data_manager -t developer`. Application logs are at
`<app URL>/logz`. `databricks bundle destroy -t developer` removes the app, the schemas and the
catalog - including their data.

### Create the app's own tables once (run this after every deploy)

The bundle creates the `_catalog` **schema**, not the registry and audit **tables** inside it.
The app creates those itself on first write - but only for a caller holding `CREATE TABLE` on
`_catalog`, which `resources/schemas.yml` grants to the administrator groups only. So whoever
opens the app first decides what happens: an admin creates the tables for everybody, while an
editor gets a warning in the log, a lost registry entry, and a History tab quietly falling back
to the Delta change feed.

Take that dependency out by running this once, as an administrator:

```bash
python scripts/bootstrap_catalog.py --catalog <your catalog>                       # review the DDL
python scripts/bootstrap_catalog.py --catalog <your catalog> \
    --warehouse-id <id> --profile <your profile> --apply
```

The statements are generated from `src/rdm/backend/registry.py`, the single declaration both
backends build their DDL from, so the script cannot drift from what the app expects. Re-running
it with `--apply` after a release is also the upgrade path: it compares each table with the
declaration and adds the columns the release introduced (see [DATA_MODEL.md](DATA_MODEL.md)).
`CREATE TABLE IF NOT EXISTS` on its own would not — it does nothing at all to a table that
already exists — which is why the printed form is only what a *fresh* catalog needs.

## 4. Enable user authorization (on-behalf-of-user) and why

Recommended production mode (DESIGN.md §5). Without it the app talks to the warehouse as
its own service principal, which would then need the union of every user's data privileges
and become the security boundary. With it, the proxy forwards a short-lived user token in
`X-Forwarded-Access-Token`; `DatabricksBackend` opens the SQL connection with that token, so
every statement runs as the signed-in user and Unity Catalog enforces the schema grants.
The app's role logic then only decides what to render.

* Bundle deployments: already configured - `resources/app.yml` declares
  `user_api_scopes: [sql, files.files]`. `sql` allows warehouse queries under the user's
  Unity Catalog permissions; `files.files` lets the app upload, download and delete files
  in the function volumes as the user (Files API). Group memberships for the sidebar are
  read through `iam.current-user:read`, a platform default scope that some workspaces
  reject when declared explicitly - it is granted either way.
* UI: Compute -> Apps -> *your app* -> **Authorization** -> enable *User authorization*,
  tick the `sql` scope, save. The app restarts.
* CLI (apps deployed without the bundle):
  `databricks apps update <app-name> --json '{"user_api_scopes": ["sql", "files.files"]}'`
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
RDM_CATALOG=_reference_data
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

The contract suite (`tests/`, DESIGN.md §9) runs against DuckDB always and, when
`RDM_TEST_DATABRICKS=1`, against a real SQL warehouse (`tests/test_databricks_live.py`,
which creates and drops a throwaway `zz_live_*` schema). Point it at a **dev** catalog only.

```bash
export RDM_TEST_DATABRICKS=1
export RDM_CATALOG=<your dev catalog>                   # never the production catalog
export DATABRICKS_WAREHOUSE_ID=<warehouse id>
export DATABRICKS_CONFIG_PROFILE=<your profile>         # or DATABRICKS_HOST + DATABRICKS_TOKEN
python -m pytest tests/test_databricks_live.py
```

The identity running the tests needs the Administrator set on the catalog, i.e. membership
of `admin_group`. It is intentionally not part of `ci.yml`, which runs credential-free.

## 8. Deploying a production target from CI

Deployment is not wired into `ci.yml`, because it needs a workspace and credentials that
only you have. To automate it, copy the `dev` target to a `prd` target (its own workspace
host, its own catalog, `mode: production`, and `run_as` naming the service principal that
will deploy), then add a workflow job that authenticates as that service principal -
GitHub OIDC federation is the option that stores no token:

```yaml
env:
  DATABRICKS_HOST: ${{ vars.DATABRICKS_HOST }}
  DATABRICKS_AUTH_TYPE: github-oidc
  DATABRICKS_CLIENT_ID: ${{ vars.DATABRICKS_CLIENT_ID }}
  BUNDLE_VAR_warehouse_id: ${{ vars.DATABRICKS_WAREHOUSE_ID }}
```

One-time steps by a workspace or metastore admin:

* Create the production catalog (or grant the deploying service principal
  `CREATE CATALOG` on the metastore for the first deploy). A pre-created catalog is
  adopted with `databricks bundle deployment bind forms <catalog name> -t prd` before the
  first deploy.
* Grant the deploying service principal CAN_MANAGE on the production SQL warehouse (needed
  to attach it as the app resource, which grants the app's own service principal CAN_USE).
* After the first deploy, isolate the catalog and bind any consuming workspaces read-only
  ("Restricting the catalog to its workspaces" above) - as the catalog owner or a
  metastore admin.
* Make the target's `root_path` writable by the service principal.

The service principal becomes the owner of the catalog and the schemas; the admin groups
get MANAGE on both so administrators can still alter and drop tables that the app or other
users created.

## 9. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no value assigned to required variable warehouse_id` | Pass `--var="warehouse_id=..."`, export `BUNDLE_VAR_warehouse_id`, or set it in the target. |
| `User does not have CREATE CATALOG on Metastore` | Some workspaces do not grant it. Create the catalog from a workspace that does (or as a metastore admin) with the same `storage_root`, then `databricks bundle deployment bind forms <catalog name> -t <target>` and redeploy. |
| `User needs MANAGE permission on the resource` when creating the app | Attaching the SQL warehouse as an app resource requires CAN_MANAGE on it for the deploying identity (§1). Ask a workspace admin to grant it. |
| `Invalid update mask ... forward_user_access_token` on app update | The CLI's embedded provider sends a field the workspace's Apps API no longer accepts; upgrade the Databricks CLI (fixed in v1.15). |
| Catalog not visible from a workspace that should read it | The catalog is `ISOLATED` and that workspace has no binding. Add it (read-only for consumers): see "Restricting the catalog to its workspaces". |
| `apps do not support a setting a run_as user that is different from the owner` | The target is being deployed with the credentials of a different identity than `run_as`. Use the service principal's own credentials (or remove `run_as` in a personal target). |
| Deploy fails: catalog already exists | The bundle creates the catalog and cannot adopt an existing one silently. Choose another `catalog` value, or bind the existing one: `databricks bundle deployment bind forms <catalog name> -t <target>` (see `databricks bundle deployment bind --help`). |
| App starts but every query fails with a permission error on the warehouse | The **user** (with user authorization) or the app's service principal (without) lacks CAN USE on the warehouse - §4. The bundle only grants the service principal. |
| `X-Forwarded-Access-Token` missing / app runs as the service principal / "insufficient scope" | User authorization not enabled, the `sql` scope not declared, or the user has not consented yet. Check the app's Authorization tab; users must reload and accept the consent screen. Scopes added later require re-consent. |
| `No user identity headers found` | `RDM_AUTH=databricks` outside the Apps proxy. Use `databricks apps run-local` or `RDM_AUTH=mock` locally. |
| Sidebar shows no functions for a user who "should" have access | The user's group has no `USE_SCHEMA`+`SELECT` on the schema, the group is workspace-local instead of account-level, or the grant was added outside the bundle and reverted by a deploy. Fix `resources/schemas.yml` and redeploy. |
| Group memberships missing in the sidebar (roles look wrong) | `iam.current-user:read` scope missing (user authorization) or, in service-principal mode, the service principal cannot read users. Roles are still enforced by Unity Catalog; only rendering is affected. |
| `Metastore storage root URL does not exist` on deploy | The metastore has no default storage root; set the `storage_root` variable (the catalog's managed location) for the target. |
| `Could not find principal with name users` on a grant | Unity Catalog grants only accept account-level principals; use `account users` (or an account group), not the workspace-local `users` group. |
| Uploading a file fails with a permission error, or files are listed without sizes | The user lacks `WRITE VOLUME` (upload/replace/delete) or `READ VOLUME` (list/download) on the function schema, the `files.files` scope is not declared, or the `_files` volume could not be created (`CREATE VOLUME`). Files landed outside the app are listed but unregistered until an admin describes them. |
| A function shows under "Unassigned" | Its schema has no `rdm.domain` property / `rdm_domain` tag (created by the bundle without it, or outside the app). A global admin assigns the domain on the function page. |
| Catalog name `_reference_data` (leading underscore) | Valid Unity Catalog name and a valid unquoted identifier in Databricks SQL; the backend quotes every identifier with backticks anyway. Some organisations reserve leading underscores for system objects in their naming policy - check yours, and note the app itself rejects leading underscores for *function* and *domain* names. |
| App status `UNAVAILABLE` / crash loop after deploy | Open `<app URL>/logz`. Usual causes: dependency pin in `requirements.txt` incompatible with the Apps Python runtime, or a `PORT`/`DATABRICKS_APP_PORT` override in `app.yaml`/`config` (the runtime sets the port; `app.py` reads it). |
| `DATABRICKS_WAREHOUSE_ID (or DATABRICKS_HTTP_PATH) must be set` | The `sql-warehouse` app resource is missing or its key differs from `valueFrom`/`value_from`. Check `resources/app.yml` (bundle) or the app's Resources tab (manual deploy). |

## 10. Publish functions in Discover domains

Unity Catalog has no *domain* securable, so the app's domain list is its own registry table
`_catalog.domains`, and a function's domain is a schema property (`rdm.domain`) plus a schema
tag (`rdm_domain`).

> **Waiting on Databricks.** Native Unity Catalog domains are not available today. Until they
> are, the registry plus the tag sync below is the bridge; when they arrive, the registry, the
> `rdm.domain` property and this sync are intended to be replaced by the native objects. Keep
> the app's domain names aligned with the organisation's published domain names.

The workspace **Discover** page groups assets into *domains* (`domain:<Name>` in search).
Domains are built on governed tags: an asset belongs to a domain when it carries the
domain's tag. The app's own domain classifier (`rdm.domain` on each function schema) is
independent of that, so the two are bridged by tagging:

1. A curator creates and publishes the domain card once on the **Discover** page (UI only;
   the governed tag of the same name is created with it), for each domain the app uses.
2. An account admin grants the syncing identity the **ASSIGN** permission on those tag
   policies (`roles/tagPolicy.assigner` in the account access-control rule set
   `accounts/<account id>/tagPolicies/<policy id>/ruleSets/default`, granted per domain
   tag to an account group); APPLY TAG on the schemas comes with the bundle's admin
   grants.
3. Run the sync after deploying or after assigning domains in the app:

   ```bash
   python scripts/sync_domain_tags.py --catalog <catalog name> --profile <your profile> [--include-tables] [--dry-run]
   ```

   It matches each schema's `rdm.domain` to a governed tag case-insensitively with
   underscores as spaces (`customer_service` -> `Customer Service`), skips schemas
   whose domain has no tag yet, and is idempotent. `--include-tables` also tags every
   form so individual tables surface under the domain filter.

Without the ASSIGN grant, assets can still be added manually: **Discover** page > domain >
**Add to Domain**, or by applying the governed tag in Catalog Explorer.
