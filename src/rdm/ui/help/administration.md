## Administration guide (Global admins)

This section is only shown to global administrators. It describes how the app maps onto
Databricks and what the data platform team has to set up.

### The hierarchy

**Domain** > **Function** > **Form**. A domain is a business classifier (aligned with the
organisation's data domains, the classification Databricks calls *domains*); a function is
a Unity Catalog schema; a form is a Delta table.

| App concept | Databricks object |
|---|---|
| Catalog | The Unity Catalog catalog configured by `RDM_CATALOG` (default `_forms`) |
| Domain | A row of the `_catalog.domains` registry (name, display name, description, owner). Not a securable: it groups functions. Each function's domain is recorded as the schema property `rdm.domain` and the schema tag `rdm_domain`, so it is visible and searchable in Catalog Explorer. |
| Function | A schema in the catalog. Description = schema `COMMENT`; display name, owner, documentation link and domain = schema `DBPROPERTIES` (`rdm.display_name`, `rdm.owner`, `rdm.doc_link`, `rdm.domain`) and tags |
| Form | A Delta table in the function schema, created with Change Data Feed, column mapping and deletion vectors. Description = table `COMMENT`; column config = `TBLPROPERTIES ('rdm.column_config')` |
| Registry | `_catalog.domains`, `_catalog.functions` and `_catalog.forms`: one row per domain / function / form with display name, description, owner, documentation link, created / updated by |
| Audit trail | `_catalog.change_log`: one row per changed row and save, with before / after images (also the source of the per-row history and restore) |

### Roles and Unity Catalog privileges

| Role | Privileges on the function schema | Who grants it |
|---|---|---|
| Viewer | `USE SCHEMA`, `SELECT` | Function admin (from the function page) or the asset bundle |
| Editor | Viewer + `MODIFY` | Function admin or the asset bundle |
| Function admin | Editor + `CREATE TABLE`, `MANAGE`, `APPLY TAG` | Global admin or the asset bundle |
| Global admin | `USE CATALOG`, `CREATE SCHEMA`, `MANAGE` on the catalog (or catalog ownership) | Metastore / catalog admin, through the asset bundle |

Only global admins delete functions (`DROP SCHEMA`, refused while the schema still holds
tables) and forms (`DROP TABLE`), and only global admins maintain the domain list and assign
functions to domains. Function admins create and change forms but cannot delete them.

Grants are made to **groups only**. Every group also needs `USE CATALOG` on the catalog;
the app tries to grant it when a role is given and reports when it lacks the right to do so.
The function page shows the effective grants read from `information_schema.schema_privileges`.

The app runs with **user authorization** (scope `sql`): every SQL statement is executed as
the signed-in user, so Unity Catalog is the enforcement point and the app only decides what
to render. Roles are resolved inside the SQL session with `current_user()` and
`is_account_group_member()`.

### Setting up domains and functions

1. **Domains** (sidebar): create the domain list once, aligned with the organisation's data
   domains. A domain can only be deleted when no function is assigned to it.
2. **New function**: pick the domain, give the function a name (`<domain>__<area>`, becomes
   the schema name), a display name, owner and the project documentation link. Functions can
   also be declared in `resources/schemas.yml` (property `rdm.domain`) and deployed by the
   bundle; assign a domain later from the function page if it was created outside the app.
3. Grant the function's groups: readers as Viewer, stewards as Editor, the owning team's
   admin group as Function admin.
4. Function admins create forms from Excel.

### Operations

* Deployment, environments and CI: `docs/DEPLOYMENT.md` in the repository.
* History depends on `_catalog.change_log`; Delta Change Data Feed on each form is the
  fallback and the input for downstream SCD Type 2 pipelines.
* The row limit per grid is `RDM_MAX_ROWS` (default 5,000); larger lists are searched
  server-side.
* Local development uses DuckDB and a persona switcher (Global admin, Function admin,
  Editor, Viewer); nothing local touches the workspace.
