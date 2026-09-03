## Finding a form or a file

* The sidebar lists every **function** you have access to, grouped under its **domain**, with
  the forms and files inside each function. Type in the search box to filter by name,
  description or owner; functions stay visible while any of their forms or files match.
* A **form** is a list you edit in a grid. A **file** (CSV or Parquet) is a dataset too large
  for a grid: you preview its first rows, download it and, as an editor, replace it as a whole.
* Your role in each function is shown as a badge: **Viewer** (read only), **Editor** (change
  rows) or **Function admin** (also create forms and change their definition).
* Click a **domain** heading (sidebar or home page) for the domain overview: its functions
  with their forms and files, and who owns what.
* Long names? Drag the right edge of the sidebar to widen it (double-click the edge to reset).
* Every form and file has its own address, so you can bookmark it or paste the link in an
  email. The **Databricks path** next to the title (and the query snippets in **Settings**)
  copy with one click for use in notebooks and SQL queries.

## Editing rows (Editors)

| Action | How |
|---|---|
| Change a value | Double-click the cell (or select it and start typing), then press **Enter**. Enter moves down, Tab moves right. |
| Pick from a list | Columns with *allowed values* open a dropdown. |
| Add a row | **Add row** inserts an empty row at the top; fill in at least the required columns. |
| Open one row | Tick a row and press **Open row**: every column of that row in a dialog (handy for wide lists), with the row's own history. |
| Set many rows at once | Tick the rows, press **Bulk update**, choose the column and the value (or *clear*), then **Apply**. |
| Delete rows | Tick the boxes in the first column, then **Delete selected**. |
| Undo | **Ctrl+Z** while editing; **Discard** throws away everything since the last save. |
| Save | **Save** writes all your changes at once. It stays disabled while a problem is listed and stays enabled while you look at other tabs. |
| Sort / filter | Click a column header to sort; use the funnel icon for column filters. The search box above the grid searches every column on the server. |
| Export | **Download** gives a CSV of what you see or an Excel file of the whole list. |
| Import | **Import rows** appends rows from an Excel or CSV file; headers are matched to column names. |

Problems (missing required value, wrong type, value not in the allowed list, duplicate key)
are listed under the grid and the cell is highlighted. Fix them before saving. Bulk updates,
item-form edits and restored versions join the same list of unsaved changes: nothing is
written until you press **Save**.

### When someone else changed the same row

Each row remembers its version. If another person saved a change to a row you also edited,
your change to that row is **not** applied and the grid tells you which rows were affected;
the grid refreshes so you can redo the edit on the current values.

## Files

| Action | Who | How |
|---|---|---|
| Preview | everyone with access | The **Preview** tab shows the first rows; **Columns** the inferred types. |
| Download | everyone with access | **Download** on the file page. |
| Replace | Editors | **Replace file**: upload a new file of the same format; the previous size and row count stay in the **History** tab. |
| Add | Function admins | **New file** on the function page or in the sidebar: choose the function, upload, check the preview, give it a name and description. |
| Delete | Global admins | **Settings** tab of the file. |

Files larger than the upload limit are landed in the function's volume directly (Databricks
CLI or a pipeline) and appear on the function page automatically, marked *not registered*
until an admin gives them a description.

## History and restore

The **History** tab shows every saved change: who, when, added / edited / deleted, and the
row values. Use the search box above it to find a row.

* **One row**: tick it, press **Open row** and scroll to *History of this row*. **Restore**
  next to a version puts that version's values back on the row (as unsaved changes).
* **Deleted rows**: on the History tab, tick the deletion entry and press **Restore selected
  version**; the row comes back as a new row on the Data tab. Press **Save** to persist.

## Roles

| Role | Can |
|---|---|
| Viewer | Open forms and files, search, export, download, open a row, read history |
| Editor | Viewer + add, change and delete rows, bulk update, import rows, restore versions, replace files |
| Function admin | Editor + create forms and add files in the function, change column descriptions / rules, add and remove columns, edit file details, manage who has access to the function |
| Global admin | Everything, in every function, plus create functions, maintain the domain list, delete functions, forms and files, and read the administration guide |

Access is granted to **groups**, never to individual accounts. Ask the function owner (shown
at the top of the function page) if you need a different role.
