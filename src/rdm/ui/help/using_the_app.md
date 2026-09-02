## Finding a form

* The sidebar lists every **domain** you have access to and the forms inside it. Type in the
  search box to filter by form name, description or owner; domains stay visible while any of
  their forms match.
* Your role in each domain is shown as a badge: **Viewer** (read only), **Editor** (change
  rows) or **Domain admin** (also create forms and change their definition).
* Every form has its own address, so you can bookmark it or paste the link in an email.

## Editing rows (Editors)

| Action | How |
|---|---|
| Change a value | Double-click the cell (or select it and start typing), then press **Enter**. Enter moves down, Tab moves right. |
| Pick from a list | Columns with *allowed values* open a dropdown. |
| Add a row | **Add row** inserts an empty row at the top; fill in at least the required columns. |
| Delete rows | Tick the boxes in the first column, then **Delete selected**. |
| Undo | **Ctrl+Z** while editing; **Discard** throws away everything since the last save. |
| Save | **Save** writes all your changes at once. It stays disabled while a problem is listed. |
| Sort / filter | Click a column header to sort; use the funnel icon for column filters. The search box above the grid searches every column on the server. |
| Export | **Download** gives a CSV of what you see or an Excel file of the whole list. |
| Import | **Import rows** appends rows from an Excel or CSV file; headers are matched to column names. |

Problems (missing required value, wrong type, value not in the allowed list, duplicate key)
are listed under the grid and the cell is highlighted. Fix them before saving.

### When someone else changed the same row

Each row remembers its version. If another person saved a change to a row you also edited,
your change to that row is **not** applied and the grid tells you which rows were affected;
the grid refreshes so you can redo the edit on the current values.

## History

The **History** tab shows every saved change: who, when, added / edited / deleted, and the
row values. Use the search box above it to find a row.

## Roles

| Role | Can |
|---|---|
| Viewer | Open forms, search, export |
| Editor | Viewer + add, change and delete rows, import rows |
| Domain admin | Editor + create forms in the domain, change column descriptions / rules, add and remove columns, manage who has access to the domain |
| Global admin | Everything, in every domain, plus create domains and read the administration guide |

Access is granted to **groups**, never to individual accounts. Ask the domain owner (shown at
the top of the domain page) if you need a different role.
