"""Scan a tree for content that must never reach a public repository.

Two rule sets are applied:

* built-in patterns that are unsafe for any organisation (workspace hosts, storage
  accounts, e-mail addresses, identifiers that look like secrets);
* the terms listed in a denylist file, which names the organisation and its estate.

The denylist file is kept outside this repository, alongside the sync procedure, so the
scanner itself can be published and run in the public repository's CI, where only the
built-in patterns apply.

    python scripts/scan_public_safe.py --root . --terms <path to the denylist>
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

SKIP_DIRS = {
    ".git",
    ".venv",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "data",
    "images",
}

SKIP_SUFFIXES = {
    ".duckdb",
    ".wal",
    ".parquet",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".xlsx",
    ".pdf",
    ".woff",
    ".woff2",
}

BUILT_IN = {
    "databricks workspace host": r"https?://[A-Za-z0-9._-]*\.(?:azuredatabricks\.net|cloud\.databricks\.com|gcp\.databricks\.com)",
    "databricks workspace id": r"\badb-\d{10,}\b",
    "azure storage uri": r"\babfss://[^\s\"'<>]+",
    "aws s3 uri": r"\bs3[an]?://[^\s\"'<>]+",
    "e-mail address": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "uuid": r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
    "windows user path": r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+",
}

# Placeholders and public references that the built-in patterns would otherwise flag.
ALLOWED = {
    "example.com",
    "example.org",
    "@example.com",
    "you@example.com",
    "owner@example.com",
    "steward@example.com",
    "noreply@github.com",
    "https://<your-workspace-host>.cloud.databricks.com",
    "abfss://<container>@<storage-account>.dfs.core.windows.net/<path>",
}


def _is_allowed(match: str, allowed: set[str]) -> bool:
    lowered = match.lower()
    return any(entry in lowered or lowered in entry for entry in allowed)


def _load_lines(path: Path | None) -> list[str]:
    if path is None or not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return lines


def _files(root: Path) -> list[Path]:
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        found.append(path)
    return found


def scan(root: Path, terms: list[str], allowed: set[str]) -> list[tuple[Path, int, str, str]]:
    rules = [(label, re.compile(pattern)) for label, pattern in BUILT_IN.items()]
    if terms:
        rules.append(("denylisted term", re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)))

    hits: list[tuple[Path, int, str, str]] = []
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        for label, rule in rules:
            for match in rule.finditer(relative):
                if not _is_allowed(match.group(0), allowed):
                    hits.append((path, 0, f"path / {label}", match.group(0)))
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            for label, rule in rules:
                for match in rule.finditer(line):
                    if not _is_allowed(match.group(0), allowed):
                        hits.append((path, number, label, match.group(0)))
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="tree to scan (default: current directory)")
    parser.add_argument("--terms", default=".public-safe-terms.txt", help="denylist file; skipped when absent")
    parser.add_argument("--allow", default=".public-safe-allow.txt", help="extra allowed literals; skipped when absent")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    terms = _load_lines(Path(args.terms) if args.terms else None)
    allowed = ALLOWED | {entry.lower() for entry in _load_lines(Path(args.allow) if args.allow else None)}

    hits = scan(root, terms, allowed)
    if not hits:
        scope = f"{len(terms)} denylisted terms" if terms else "built-in patterns only"
        print(f"scan_public_safe: clean ({root}, {scope}).")
        return 0

    for path, number, label, match in hits:
        where = path.relative_to(root).as_posix()
        print(f"{where}:{number}: {label}: {match}")
    print(f"\nscan_public_safe: {len(hits)} finding(s) must be resolved before publishing.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
