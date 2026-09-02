"""Streamlit entrypoint for the Reference Data Manager (local and Databricks Apps)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from rdm.ui.app import main  # noqa: E402

main()
