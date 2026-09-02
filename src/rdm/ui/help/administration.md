## Administration guide (Global admins)

This section is only shown to global administrators. It describes how the app maps onto
Databricks and what the data platform team has to set up.

### Where things live

| App concept | Databricks object |
|---|---|
| Catalog | The Unity Catalog catalog configured by `RDM_CATALOG` (default `_forms`) |
| Domain | A schema in that catalog. Description = schema `COMMENT`; display name, owner and documentation link = schema `DBPROPERTIES` (`rdm.display_name`, `rdm.owner`, `rdm.doc_link`) and tags |
| Form | A Delta table in the domain schema, created with Change Data Feed, column mapping and deletion vectors. Description = table `COMMENT`; column config = `TBLPROPERTIES ('rdm.column_config')` |
| Registry | `_catalog.domains` and `_catalog.forms`: one row per domain / form with display name, description, owner, documentation link, created / updated by |
| Audit trail | `_catalog.change_log`: one row per changed row and save, with before / after images |

### Roles and Unity Catalog privileges

| Role | Privileges on the domain schema | Who grants it |
|---|---|---|
| Viewer | `USE SCHEMA`, `SELECT` | Domain admin (from the domain page) or the asset bundle |
| Editor | Viewer + `MODIFY` | Domain admin or the asset bundle |
| Domain admin | Editor + `CREATE TABLE`, `MANAGE`, `APPLY TAG` | Global admin or the asset bundle |
| Global admin | `USE CATALOG`, `CREATE SCHEMA`, `MANAGE` on the catalog (or catalog ownership) | Metastore / catalog admin, through the asset bundle |

Grants are made to **groups only**. Every group also needs `USE CATALOG` on the catalog;
the app tries to grant it when a role is given and reports when it lacks the right to do so.
The domain page shows the effective grants read from `information_schema.schema_privileges`.

The app runs with **user authorization** (scope `sql`): every SQL statement is executed as
the signed-in user, so Unity Catalog is the enforcement point and the app only decides what
to render. Roles are resolved inside the SQL session with `current_user()` and
`is_account_group_member()`.

### Setting up a new domain

1. Global admin creates it here (**New domain**) or adds it to `resources/schemas.yml` in
   the asset bundle and deploys. Give it a display name, owner and the project documentation
   link.
2. Grant the domain's groups: readers as Viewer, stewards as Editor, the owning team's admin
   group as Domain admin.
3. Domain admins create forms from Excel.

### Operations

* Deployment, environments and CI: `docs/DEPLOYMENT.md` in the repository.
* History depends on `_catalog.change_log`; Delta Change Data Feed on each form is the
  fallback and the input for downstream SCD Type 2 pipelines.
* The row limit per grid is `RDM_MAX_ROWS` (default 5,000); larger lists are searched
  server-side.
* Local development uses DuckDB and a persona switcher; nothing local touches the workspace.
