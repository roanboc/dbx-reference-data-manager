## Finding a form

* The sidebar lists every **function** you have access to, grouped under its **domain**, with
  the forms inside each function. Type in the search box to filter by form name, description
  or owner; functions stay visible while any of their forms match.
* Your role in each function is shown as a badge: **Viewer** (read only), **Editor** (change
  rows) or **Function admin** (also create forms and change their definition).
* Every form has its own address, so you can bookmark it or paste the link in an email.

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
| Viewer | Open forms, search, export, open a row, read history |
| Editor | Viewer + add, change and delete rows, bulk update, import rows, restore versions |
| Function admin | Editor + create forms in the function, change column descriptions / rules, add and remove columns, manage who has access to the function |
| Global admin | Everything, in every function, plus create functions, maintain the domain list, delete functions and forms, and read the administration guide |

Access is granted to **groups**, never to individual accounts. Ask the function owner (shown
at the top of the function page) if you need a different role.
