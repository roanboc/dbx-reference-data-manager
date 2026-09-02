## Creating a form (Domain admins)

**New form** starts a four-step wizard.

1. **Source** - upload an Excel or CSV file, or start from scratch. From a file, the first
   sheet's header row becomes the column names and the types are inferred from the values.
2. **Columns** - confirm the definition of every column (see below).
3. **Details** - choose the domain, the table name (lower_snake_case, becomes the Unity
   Catalog table name), a display name, a description and an owner.
4. **Review** - check the definition and the rows that will be loaded, then **Create form**.

### The columns step

| Column | Meaning |
|---|---|
| Column name | Technical name: lower-case letters, digits and underscores (`cost_centre_code`). The display name in the grid is derived from it (`Cost Centre Code`). |
| Type | `STRING` text, `INTEGER` whole numbers, `DECIMAL` money and other fixed-precision numbers, `DOUBLE` floating point, `BOOLEAN` yes/no, `DATE`, `TIMESTAMP` date and time. Types cannot be changed after the form exists. |
| Description | Shown as a tooltip on the column header. Use it to explain what the value means. |
| Required | The value can never be empty. |
| Business key | The column (or combination of columns) that identifies a row, e.g. a code. The app refuses duplicates. |
| Allowed values | A comma-separated list, e.g. `Active, Inactive, Retired`. The column becomes a dropdown and other values are rejected. Text columns only. When you upload a file, low-cardinality text columns get a suggestion you can edit or clear. |
| Sample values | Read only: the first values found in your file, so you can check the inferred type. Empty when starting from scratch. |

Every form also gets the system columns `_id`, `_version`, `_created_at`, `_created_by`,
`_updated_at` and `_updated_by`. They are managed by the app and shown through the
**Audit columns** toggle.

### After creation

* **Schema** tab: change descriptions, required flags, business keys and allowed values; add
  or remove columns. Type and name changes are not offered because they would rewrite the
  table - add a new column and migrate instead.
* **Settings** tab: display name, description, owner, and deleting the form.
* **Import rows** appends rows from a file; the mapping uses the column names, so download the
  Excel export first if you want a template.
