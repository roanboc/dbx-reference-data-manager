"""Create the app's ``_catalog`` tables once, as an administrator.

The app creates the registry and audit tables itself with ``CREATE TABLE IF NOT EXISTS`` the
first time it writes to them — but only an identity that holds ``CREATE TABLE`` on ``_catalog``
can do that, and ``resources/schemas.yml`` deliberately grants that to the administrator groups
only. So whoever opens the app *first* decides whether the tables come into being: an admin
creates them for everyone, an editor gets a warning in the log, a lost registry entry and a
History tab quietly falling back to the Delta change feed.

Running this once after ``databricks bundle deploy``, as an administrator, removes that
dependency on who happens to click first.

    # review the DDL without connecting to anything
    python scripts/bootstrap_catalog.py --catalog _reference_data

    # create the tables
    python scripts/bootstrap_catalog.py --catalog _reference_data \\
        --warehouse-id <id> --apply [--profile <your CLI profile>]

The statements come from ``rdm.backend.registry``, the single declaration both backends
generate their DDL from, so this script cannot drift from what the app expects. It is
idempotent: every statement is ``IF NOT EXISTS``, and re-running it after a release adds the
columns a newer declaration introduced.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rdm.backend import registry  # noqa: E402
from rdm.models import validate_identifier  # noqa: E402


def statements(catalog: str, meta_schema: str = "_catalog") -> list[str]:
    """Every statement needed to bring ``<catalog>.<meta_schema>`` up to the declaration."""
    validate_identifier(catalog, "catalog name")
    validate_identifier(meta_schema, "schema name")
    schema = f"`{catalog}`.`{meta_schema}`"
    out = [
        f"CREATE SCHEMA IF NOT EXISTS {schema} "
        f"COMMENT 'Reference Data Manager registry and audit trail (managed by the app)'"
    ]
    out += [t.databricks_ddl(f"{schema}.`{t.name}`") for t in registry.REGISTRY_TABLES]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--catalog", default="_reference_data", help="the reference-data catalog")
    parser.add_argument("--meta-schema", default="_catalog", help="the app's system schema")
    parser.add_argument("--warehouse-id", help="SQL warehouse to run the statements on (with --apply)")
    parser.add_argument("--profile", help="Databricks CLI profile (default: ambient SDK auth)")
    parser.add_argument("--apply", action="store_true", help="run the statements instead of printing them")
    args = parser.parse_args()

    ddl = statements(args.catalog, args.meta_schema)
    if not args.apply:
        print("-- Review these, then re-run with --apply (or hand them to a workspace admin).")
        for statement in ddl:
            print(f"{statement};\n")
        return 0

    if not args.warehouse_id:
        parser.error("--apply needs --warehouse-id")

    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    for statement in ddl:
        head = statement.split("(", 1)[0].strip()
        response = w.statement_execution.execute_statement(
            statement=statement, warehouse_id=args.warehouse_id, catalog=args.catalog
        )
        state = response.status.state.value if response.status and response.status.state else "UNKNOWN"
        if state not in {"SUCCEEDED", "PENDING", "RUNNING"}:
            error = response.status.error.message if response.status and response.status.error else state
            print(f"  FAILED  {head}\n          {error}")
            return 1
        print(f"  ok      {head}")
    print(f"\n{args.catalog}.{args.meta_schema} is ready.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
