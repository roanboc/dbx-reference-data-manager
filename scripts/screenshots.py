"""Capture the README screenshots from a locally running app (``python app.py --dev``).

Renders the same pages in light and dark by emulating the OS colour scheme, which is what
the app follows (MantineProvider ``defaultColorScheme="auto"``, AG Grid quartz auto-dark).

    python scripts/screenshots.py [--base http://127.0.0.1:8050] [--out docs/images]
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

#: (path, name, ready selector or "networkidle", schemes) — README needs one dark shot only.
SHOTS = [
    ("/", "home", "networkidle", ("light",)),
    ("/f/finance__cost_management/cost_centres", "form", ".ag-cell", ("light", "dark")),
    ("/fn/finance__cost_management", "function", "networkidle", ("light",)),
    ("/new-form", "creator", "networkidle", ("light",)),
]


def capture(page: Page, base: str, out: Path, scheme: str) -> None:
    for path, name, ready, schemes in SHOTS:
        if scheme not in schemes:
            continue
        page.goto(base + path)
        if ready == "networkidle":
            page.wait_for_load_state("networkidle")
        else:
            page.wait_for_selector(ready)
        page.add_style_tag(content='[class*="dash-debug"] { display: none !important; }')
        page.wait_for_timeout(600)  # icons and grid polish
        target = out / f"{name}-{scheme}.png"
        page.screenshot(path=str(target))
        print(f"wrote {target}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8050")
    parser.add_argument("--out", default="docs/images")
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        for scheme in ("light", "dark"):
            context = browser.new_context(viewport={"width": 1440, "height": 860}, color_scheme=scheme)
            capture(context.new_page(), args.base.rstrip("/"), out, scheme)
            context.close()
        browser.close()


if __name__ == "__main__":
    main()
