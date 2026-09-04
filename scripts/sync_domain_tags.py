"""Assign each function schema to its Databricks Discover domain (docs: uc-semantics/domains).

Discover-page domains are built on governed tags: an asset belongs to the domain whose tag
it carries. The app records its business domain on every function schema as the
``rdm.domain`` property; this script bridges the two by tagging each schema (and,
optionally, its tables) with the governed tag that matches the domain, so functions and
forms show up under ``domain:<Name>`` in workspace search.

The domain card itself (create, describe, publish) is managed on the Discover page by a
curator; this script only maintains the asset assignments. The identity running it needs
APPLY TAG on the schemas and the ASSIGN (or MANAGE) permission on each domain's tag policy,
granted by an account admin.

    python scripts/sync_domain_tags.py --catalog _reference_data --profile <your profile> [--include-tables] [--dry-run]
"""

from __future__ import annotations

import argparse

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import EntityTagAssignment


def matching_tag(domain: str, tag_keys: list[str]) -> str | None:
    """The governed tag for an rdm.domain value: `finance` -> `Finance`, `customer_service` -> `Customer Service`."""
    wanted = domain.replace("_", " ").casefold()
    return next((k for k in tag_keys if k.casefold() == wanted), None)


def ensure_assignment(
    w: WorkspaceClient, entity_type: str, entity_name: str, tag_key: str, dry_run: bool
) -> None:
    try:
        w.entity_tag_assignments.get(entity_type, entity_name, tag_key)
        print(f"  ok      {entity_type[:-1]} {entity_name} already in domain {tag_key}")
        return
    except NotFound:
        pass
    if dry_run:
        print(f"  would   assign {entity_type[:-1]} {entity_name} to domain {tag_key}")
        return
    w.entity_tag_assignments.create(
        EntityTagAssignment(entity_type=entity_type, entity_name=entity_name, tag_key=tag_key)
    )
    print(f"  tagged  {entity_type[:-1]} {entity_name} -> domain {tag_key}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", required=True, help="reference-data catalog to sync")
    parser.add_argument("--profile", help="Databricks CLI profile (default: ambient SDK auth)")
    parser.add_argument("--include-tables", action="store_true", help="also tag every table in each schema")
    parser.add_argument("--dry-run", action="store_true", help="report without assigning")
    args = parser.parse_args()

    w = WorkspaceClient(profile=args.profile) if args.profile else WorkspaceClient()
    tag_keys = [p.tag_key for p in w.tag_policies.list_tag_policies() if p.tag_key]

    for schema in w.schemas.list(catalog_name=args.catalog):
        domain = (schema.properties or {}).get("rdm.domain")
        if not domain or not schema.name:
            continue
        tag_key = matching_tag(domain, tag_keys)
        print(f"{schema.name} (rdm.domain={domain})")
        if not tag_key:
            print(
                f"  skip    no governed tag matches domain {domain!r}; create it on the Discover page first"
            )
            continue
        ensure_assignment(w, "schemas", f"{args.catalog}.{schema.name}", tag_key, args.dry_run)
        if args.include_tables:
            for table in w.tables.list(catalog_name=args.catalog, schema_name=schema.name):
                if table.full_name:
                    ensure_assignment(w, "tables", table.full_name, tag_key, args.dry_run)


if __name__ == "__main__":
    main()
