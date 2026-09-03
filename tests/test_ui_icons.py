"""Icons are vendored SVGs: every name used in the UI must exist locally (no CDN at runtime)."""

from __future__ import annotations

import re
from pathlib import Path

from rdm.ui.components import icon

ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "assets" / "icons" / "tabler"
NAME_RE = re.compile(r"tabler:([a-z0-9-]+)")


def used_icons() -> set[str]:
    return {m for p in (ROOT / "src").rglob("*.py") for m in NAME_RE.findall(p.read_text(encoding="utf-8"))}


def test_every_icon_used_in_the_ui_is_vendored():
    names = used_icons()
    assert names, "no icons found in src"
    missing = sorted(n for n in names if not (ICON_DIR / f"{n}.svg").exists())
    assert not missing, f"run scripts/vendor_icons.py for: {missing}"
    css = (ROOT / "assets" / "icons.css").read_text(encoding="utf-8")
    assert all(f".rdm-icon-{n} " in css for n in names), (
        "assets/icons.css is stale: run scripts/vendor_icons.py"
    )


def test_icon_component_uses_the_css_class_and_mantine_colours():
    span = icon("tabler:search", 20, color="teal")
    assert span.className == "rdm-icon rdm-icon-search"
    assert span.style == {"width": "20px", "height": "20px", "color": "var(--mantine-color-teal-filled)"}
    assert "color" not in icon("tabler:search").style
