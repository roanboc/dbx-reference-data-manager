#!/usr/bin/env python
"""Create (or recreate with --reset) the local DuckDB database with demo domains, functions and forms.

Usage: python scripts/seed_demo.py [--reset] [--path data/rdm.duckdb]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rdm.backend.duckdb_backend import DuckDBBackend  # noqa: E402
from rdm.config import Settings  # noqa: E402
from rdm.demo import seed  # noqa: E402


def main() -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", default=settings.duckdb_path)
    parser.add_argument("--reset", action="store_true", help="delete the existing database first")
    args = parser.parse_args()
    path = Path(args.path)
    if path.exists():
        if not args.reset:
            print(f"{path} already exists; use --reset to recreate it.")
            return 1
        path.unlink()
        wal = path.with_suffix(path.suffix + ".wal")
        if wal.exists():
            wal.unlink()
    backend = DuckDBBackend(str(path))
    seed(backend)
    domains = backend.list_domains()
    functions = backend.list_functions()
    print(f"Seeded {path} with {len(domains)} domains and {len(functions)} functions:")
    for f in functions:
        print(f"  - {f.domain or '(unassigned)'} > {f.name} ({f.form_count} forms)")
    backend.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
