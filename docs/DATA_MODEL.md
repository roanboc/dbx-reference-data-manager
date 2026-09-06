# Data model — how it is meant to grow

| | |
|---|---|
| Status | Reviewed and hardened; the contract below is enforced by tests |
| Audience | Anyone adding a field, a rule, an option or an object kind |
| Related | [DESIGN.md](DESIGN.md) (architecture and decision log), [FUNCTIONAL_DESIGN.md](FUNCTIONAL_DESIGN.md) (requirements), [AGENTS.md](../AGENTS.md) (invariants) |

[DESIGN.md](DESIGN.md) says what the model *is*. This document says how it is allowed to
**change**, because that is the property the app has to keep: reference data outlives the
release that created it, two versions of the app can meet the same catalog during a rolling
deploy, and a pipeline or a data engineer can write to the same tables with the CLI.

The model is deliberately small. The goal is not to make it bigger; it is to make sure the
next field, rule or option is an *additive* change to one declaration rather than a migration
of a catalog that already holds someone's reference data.

## 1. The one rule

> **Additive only.** Add keys and columns; never rename, retype or remove one. Anything you
> do not understand, you keep.

Everything below is a consequence of it.

| | |
|---|---|
| **Add, never change** | A new rule is a new key; a new registry field is a new nullable column. Renaming or retyping breaks every reader that is not deployed at the same instant. |
| **Keep what you do not know** | A reader stores the keys it does not recognise and writes them back untouched, so an older build editing a description cannot silently strip what a newer build stored. |
| **Reads are shape-tolerant** | Registry reads are `SELECT *` zipped with the driver's column names; a catalog with columns this build has never heard of is read without error. |
| **Writes name their columns** | No positional `INSERT ... VALUES`. A writer that depends on arity breaks the day a column is added. |
| **Nullable by definition** | A column added today has no value in rows written yesterday, so it cannot be `NOT NULL`. |
| **Fail closed, never crash** | Metadata the app cannot parse leaves defaults in place; a role name it does not know grants nothing. Bad metadata must never make an object unopenable. |

`METADATA_VERSION` (`models.py`) stamps every document the app writes. Because the contract is
additive, the version only has to move when a reader must behave *differently* — which is rare,
and is exactly the case the stamp exists to make visible.

## 2. Where a change goes

Four extension points, in order of how cheap they are. Reach for the cheapest one that fits.

### 2.1 A rule about one column → `rdm.column_config`

Allowed values and business keys live here today. A pattern, a default, a unit, a display
label, a sensitivity classification or a lookup reference belongs here too.

```python
# models.py
COLUMN_RULE_KEYS = frozenset({"options", "key", "pattern"})   # <- the whole storage change
```

Then a field on `ColumnDef`, one line in `FormDef.column_config()` and one in
`apply_column_config()`. **The backends do not change**: they carry the document as an opaque
string. Keys this build does not list are preserved on `ColumnDef.extra` and written back
verbatim, so a newer build's rule survives an older build's save.

Two boundaries worth keeping:

* What the platform can enforce or display stays **native** — `nullable` is `NOT NULL`,
  `description` is a column `COMMENT`, the type is the column type. Every consumer of the
  table then sees them without knowing anything about this app.
* A rule that **references another object** (a lookup into another form) should not be buried
  in one table's property, because nothing could then answer "what breaks if I drop this
  form?". Mirror it into the registry when the first such rule lands.

### 2.2 An option about one form → `rdm.settings`

A separate document for things that are neither a column rule nor a description: an approval
step (FR-23), a retention rule, a notification, a lifecycle state. Add the key to
`SETTINGS_KEYS`; storage does not change at all, because the document already round-trips
every key it is given.

It is a **separate property** from `column_config` on purpose: allowed-value lists grow, and a
form's options must not be crowded out of the property's size budget by them.

> Only forms have this today. Giving `DomainDef`, `FunctionDef` and `FileDef` the same
> document is the designed next step — see §6.

### 2.3 A field the app records about an object → `backend/registry.py`

The `_catalog` registry (domains, functions, forms, files, change_log) is declared **once** and
both dialects are generated from it:

```python
FUNCTIONS = RegistryTable(
    name="functions",
    keys=("name",),
    columns=_described(("name", TEXT), ("domain_name", TEXT), ..., ("steward", TEXT)),
    comment="...",
)
```

One entry gives you: the DuckDB DDL, the Databricks DDL, the upgrade of every existing local
database on next open, and the upgrade of a production catalog the first time this build
writes to it. `tests/test_registry.py` fails if the two dialects drift apart, if a new column
is `NOT NULL`, or if a writer goes back to a positional `INSERT`.

Add columns to `CHANGE_LOG` the same way — it already carries `object_type` (what kind of
thing changed) and `meta_json` (a free-form envelope) so that recording a definition change,
a grant change or an approval does not need a new shape.

### 2.4 The expensive ones

Two changes are not additive and should be entered deliberately:

* **A new `DataType`.** Types are *derived* from the physical column on every read rather than
  persisted, which is why the model can never drift from the table — but adding a member means
  touching the coercion, both dialect maps, the grid editors, the import inference and the
  form creator. Prefer a semantic rule in `column_config` (§2.1) over a new storage type: an
  `EMAIL` is a `STRING` with a rule, not a new type.
* **A new object kind** beside domain / function / form / file. That is a registry table, a
  page, a role check and an audit `object_type`. Nothing prevents it; just do not do it by
  accident.

## 3. Identity, and the one thing that is not extensible

| Level | Key | Consequence |
|---|---|---|
| Row | `_id`, a generated UUID, with `_version` as the concurrency token | Correct and stable. Edits map to `MERGE ... ON _id`; the business key is a validation rule, not the identity. |
| Object | its **name** (`domains.name`, `functions.name`, `(function_name, name)`) | Repeated as a foreign key in the registry, `change_log`, `object_properties`, the `_h__` table and the URL. |

Because objects are keyed by name, **names never change**: there is no rename, no move of a
form between functions, and a name that is dropped and recreated inherits the previous
object's history. That matches how the app is specified today (FR-13: names are fixed once
created) and it is a legitimate answer — but it is the one place where deferring costs
something later, so it is written down rather than assumed.

> **Open decision.** If renaming, moving a form, or a guided "retire a function" (the
> roadmap's own next item) is ever wanted, a surrogate `object_id` has to be minted *first*:
> history written before it exists can never be re-attributed to the object it belongs to.
> With the registry declaration in place this is now five one-line entries plus minting on
> create, and it needs no migration — but it also adds a field nothing reads yet, so it is
> the team's call, not a default.

## 4. What the review checked, and what it found

Every finding below was reproduced against the code before it was fixed, and each fix carries
a regression test. `tests/test_registry.py` additionally had each of its guards checked against
the regression it exists to catch.

| Found | Why it mattered | Fixed |
|---|---|---|
| `column_config` rebuilt its document from the model fields on every save, and never read the `"version"` it wrote | The app's main extension point was **closed**: a `pattern` stored by a newer build was destroyed by an older build's next description edit | Readers keep unknown keys (`ColumnDef.extra`, `FormDef.config_extra`) and write them back; the version travels with the document |
| No home for a per-form option that is not a column rule | `scd2_enabled` had to become its own property; every later option would repeat that | `rdm.settings`, a versioned document with the same contract |
| No size guard on a document stored in one table property | An over-long allowed-value list fails the whole `ALTER TABLE`, taking the unrelated metadata in the same statement with it | `MAX_DOCUMENT_BYTES`, refused with a readable message; reads are never checked, so an oversized form stays openable |
| A malformed `rdm.column_config` raised `AttributeError` | Any principal with `MODIFY` could make a form unopenable with one `SET TBLPROPERTIES` | The reader is total; the strict xfail that documented it is now a passing test |
| The registry shape was declared four times, and production self-healed by matching the literal string `"owner_email"` in a driver error | The 2nd added column would have failed silently — the write was swallowed with a log line | One declaration, generated DDL, a declarative reconcile on both sides |
| Local metadata writers used positional `INSERT ... VALUES` | Adding any `change_log` column was an immediate hard break locally | Every writer names its columns; a test keeps it that way |
| `_ensure_meta` was not transactional | A half-applied upgrade was unreachable to repair | Wrapped in a transaction |
| SCD2: disable → add a column → re-enable left `_h__<form>` one column short | Every later save failed with a raw binder error; the form was permanently unsaveable | The history table is reconciled on enable and maintained whenever it *exists*, not only while the flag is on; its inserts name their targets |
| SCD2: disabling left every validity window open | `__END_AT IS NULL` is published as "the current version", so the history advertised a superseded value — permanently, since re-enabling skipped exactly those rows | Disabling closes the open windows |
| The two audit logs had different shapes and numbered history differently | A feature built and tested locally behaved differently in production | DuckDB gained `seq`; both number history entries the same way |
| `Role[name]` raised `KeyError` on an unknown role | One grant row naming a role this build does not know broke permission resolution for everyone in that function | `Role.from_name` fails closed |
| Deleting a form erased its audit trail on DuckDB, kept it on Databricks | The trail is the governance record and outlives the object it describes (FUNCTIONAL_DESIGN §7.5); the two backends disagreed and one contradicted the spec | DuckDB keeps it, as the spec and Databricks always did |
| `INTEGER` values were parsed through `float()` | The column is a `BIGINT`: every value above 2^53 was silently rounded, with no error anywhere | Parsed exactly |
| Business-key uniqueness was order dependent | Editing a row to duplicate one *further down the loaded page* passed validation and reached the table | Every surviving row is grouped under the key it will have after the save |
| `put_file` issued `CREATE VOLUME` even when replacing | Replace is an Editor action; `CREATE VOLUME` is a Function admin privilege | Only creating a file ensures the volume |

### Things that were checked and are right as they are

* Row identity and concurrency: a generated UUID plus an integer version token compared as an
  integer, so it survives every timestamp round trip.
* Types derived from the physical column on every read, with `OTHER` as a read-only escape
  hatch for tables the app did not create — the model cannot drift from the table, and the
  guard against writing an `OTHER` column is present at every write site.
* `before_json` / `after_json` as opaque snapshots — the reason the audit trail survives any
  later change to the form it describes. Keep them opaque; new facts go in new columns beside
  them.
* Sparse metadata documents: only columns with a non-default setting appear, so "no entry"
  means "defaults", and configuration for a dropped column disappears on the next write.
* Files stay files — described by a registry row, read by pipelines straight from the volume,
  never loaded into a table. Large datasets stay entirely out of the row model.
* The catalog is a constructor parameter with a per-instance confinement guard, so a second
  RDM catalog on one metastore is a deployment choice, not a code change.

## 5. How well the model absorbs what is planned

Assessed by tracing each scenario through the code. "Additive" means new keys and columns
only; "costs work" means real code in known places; nothing on the list is *blocked* by the
persisted data, and no scenario needs a migration of user rows.

| Scenario | Cost today |
|---|---|
| A new per-column rule (pattern, default, unit, label) | Additive — §2.1, backends unchanged |
| A per-form option (approval, retention, notification) | Additive — §2.2, storage unchanged |
| A new registry field (steward, classification) | Additive — §2.3, one entry |
| A new audit fact (definition change, grant change, approval) | Additive — `object_type` / `meta_json` are already there |
| FR-23 approval step, per **form** | Costs work: the option is additive, the pending change-set and its trail are new |
| FR-23 approval step, per **file** | Costs more: files have no settings document yet (§6) |
| FR-21 lookup columns | Costs work: the rule is additive, but resolving and validating against another form is new, and the reference should be mirrored into the registry |
| FR-20 effective dating | Costs work: ordinary columns already work; a first-class pair needs UI and validation |
| A second file format (JSON, XLSX) | Additive — one entry in `FILE_FORMATS` plus a reader. A *directory-shaped* format such as Delta is refused by the volume-path guard by construction |
| A second RDM catalog on one metastore | Already supported — one bundle target per catalog |
| Native Unity Catalog domains replacing the registry list | Costs work, and is anticipated: the domain name is deliberately kept aligned with the published domain names |
| Renaming or moving an object | **Not possible** by design — see §3 |
| Localised display names | Costs work: today's display name is a single-language scalar in three places; the settings document is the natural seat once §6 lands |

## 6. Next steps for the model

In the order they pay off. All are additive; none needs a migration.

1. **Give domains, functions and files the same settings document as forms.** One `settings`
   column on three registry tables (one line each) and a shared mixin in `models.py`. This is
   what makes FR-23's "per form **or file**" and every later per-object option a one-line
   change. **Prerequisite:** the registry then holds the only copy of those options, so
   `_register_function` must become strict like `_register_file` already is — today a failed
   registry write is only logged.
2. **A repair routine for the registry** (`scripts/repair_registry.py`, modelled on
   `sync_domain_tags.py`). The registry is a deliberate denormalisation of what Unity Catalog
   holds, its writes are best-effort, and nothing today can detect or repair the drift that
   follows. The SDK can read schema and table properties in bulk, so a `--dry-run` report and
   an `--apply` backfill are straightforward. This also closes the gap where a function
   created by the **bundle** carries `rdm.display_name` / `rdm.owner` / `rdm.domain` as schema
   properties that the app's read path never looks at, so it shows up unnamed and unassigned.
3. **Mirror the per-form contract into `_catalog.forms`** (`column_config`, `settings`, `scd2`
   as three nullable columns). Form rules are readable only one `SHOW TBLPROPERTIES` at a
   time today, which is what makes a cross-form view — an approval queue, a lookup target, a
   badge on the function page — expensive. Do it *after* (2), so the drift it introduces is
   detectable.
4. **Decide on `object_id`** (§3). Cheap now, impossible to backfill later.
5. **Validate two Databricks-only details against a real workspace** before release, both
   flagged by review and neither reproducible locally: user `TIMESTAMP` columns are created
   as `TIMESTAMP` while every app-managed table uses `TIMESTAMP_NTZ` (the session is pinned to
   UTC, so this is currently harmless — but NTZ is what the app means), and the audit table
   would benefit from `CLUSTER BY (schema_name, table_name, changed_at)`, which is the kind of
   thing that is cheap at creation and expensive to add to a table with history in it.
   Both are one line in `registry.py` / `sql_utils.py` once a workspace can confirm them.
6. **Give `change_log` a growth story.** No clustering, no partitioning and a client-side
   History tab that one large import buries. `batch_id` is already written, so grouping a save
   into one entry is the cheap first move — and it is also what would let a whole save be
   undone as a unit, which the per-row restore machinery could already do.
7. **Check business keys against the table, not the page.** `_check_unique_keys` compares the
   change set with the rows the grid has loaded (`RDM_MAX_ROWS`, default 5,000). On a longer
   list a duplicate outside the loaded page is not seen, and the append import checks no keys
   at all. Unity Catalog does not enforce `PRIMARY KEY`, so the app is the only guard: one
   `SELECT` of the affected key values before the save closes it.
