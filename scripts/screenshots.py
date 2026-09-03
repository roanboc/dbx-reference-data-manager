#!/usr/bin/env python
"""Capture the README screenshots (docs/screenshots/) from a running local app.

    make seed && make serve          # in one terminal (http://localhost:8050)
    python scripts/screenshots.py    # in another; pass --base to use another address

Needs the dev requirements and a Chromium for Playwright (``playwright install chromium``, or
point RDM_TEST_BROWSER at an existing Chromium executable). A small Excel file is generated on
the fly for the "New form" wizard steps.
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

import pandas as pd
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
VIEWPORT = {"width": 1440, "height": 900}
SCHEME_INDEX = {"auto": 0, "light": 1, "dark": 2}


def demo_workbook(path: Path) -> Path:
    pd.DataFrame(
        {
            "Supplier Code": ["SUP-001", "SUP-002", "SUP-003", "SUP-004", "SUP-005", "SUP-006"],
            "Supplier Name": [
                "Northwind Traders",
                "Contoso Ltd",
                "Fabrikam Inc",
                "Adventure Works",
                "Tailspin Toys",
                "Wide World Importers",
            ],
            "Category": ["IT services", "Facilities", "IT services", "Consulting", "Facilities", "Logistics"],
            "Country": ["NL", "GB", "DE", "US", "GB", "NL"],
            "Preferred": [True, False, True, True, False, False],
            "Payment Terms (days)": [30, 45, 30, 60, 30, 45],
            "Contract Start": pd.to_datetime(
                ["2024-01-01", "2023-07-15", "2024-03-01", "2022-11-01", "2025-01-01", "2024-09-01"]
            ),
            "Annual Spend": [125000.50, 48000.0, 310500.75, 92000.0, 15500.25, 76000.0],
        }
    ).to_excel(path, index=False)
    return path


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--base", default="http://localhost:8050")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    base = args.base.rstrip("/")
    launch = {"executable_path": os.environ["RDM_TEST_BROWSER"]} if os.environ.get("RDM_TEST_BROWSER") else {}
    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as p:
        workbook = demo_workbook(Path(tmp) / "Preferred suppliers.xlsx")
        browser = p.chromium.launch(**launch)
        page = browser.new_context(viewport=VIEWPORT).new_page()

        def ready(selector: str, settle: int = 1500) -> None:
            page.wait_for_selector(selector, timeout=30000)
            page.wait_for_timeout(settle)
            page.mouse.move(0, 0)  # no tooltips in the pictures

        def scheme(mode: str) -> None:
            page.locator("#color-scheme label").nth(SCHEME_INDEX[mode]).click()
            page.wait_for_timeout(600)
            page.mouse.move(0, 0)

        def shot(name: str, **kwargs) -> None:
            page.wait_for_timeout(300)
            page.screenshot(path=str(OUT / f"{name}.png"), **kwargs)
            print(f"  {name}.png")

        checkbox = ".ag-pinned-left-cols-container .ag-row >> nth={} >> .ag-checkbox-input >> nth=0"

        page.goto(f"{base}/")
        ready("#home-cards")
        scheme("light")
        shot("home")

        page.goto(f"{base}/f/finance__cost_management/cost_centres")
        ready(".ag-root-wrapper")
        page.click("#grid-add")  # an empty row: the validation and the pending-changes bar show
        page.wait_for_timeout(1500)
        page.locator(checkbox.format(2)).check(force=True)
        page.wait_for_timeout(500)
        shot("form-grid", full_page=True)
        scheme("dark")
        shot("form-grid-dark", full_page=True)
        scheme("light")
        page.click("#grid-discard")
        page.wait_for_timeout(800)

        page.locator(checkbox.format(0)).check(force=True)
        page.wait_for_timeout(300)
        page.click("#item-open")
        page.wait_for_timeout(1500)
        shot("item-form")
        page.keyboard.press("Escape")
        page.wait_for_timeout(600)
        page.click("text=History")
        page.wait_for_timeout(2000)
        shot("form-history")

        page.goto(f"{base}/new-form/finance__cost_management")
        ready("#wizard-stepper", 1200)
        page.set_input_files("#wiz-upload input[type=file]", str(workbook))
        page.wait_for_timeout(3000)
        shot("new-form-source")
        page.click("#wiz-next")
        page.wait_for_timeout(2500)
        shot("new-form-columns")

        page.goto(f"{base}/fn/finance__cost_management")
        ready("#function-forms")
        shot("function-page")
        page.goto(f"{base}/file/finance__cost_management/gl_transactions.csv")
        ready(".ag-root-wrapper", 2000)
        shot("file-page")
        page.goto(f"{base}/dm/finance")
        ready("#domain-functions", 1200)
        shot("domain-overview")
        page.goto(f"{base}/help")
        ready("#help-tabs", 1200)
        shot("help-about")
        scheme("auto")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
