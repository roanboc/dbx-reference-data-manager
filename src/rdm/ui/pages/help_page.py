"""Help: user guidance for everyone, technical documentation for global administrators."""

from __future__ import annotations

from pathlib import Path

import dash_mantine_components as dmc
from dash import dcc

from rdm.ui import ids
from rdm.ui.components import icon, page_title
from rdm.ui.context import AppContext

HELP_DIR = Path(__file__).resolve().parents[1] / "help"


def _md(name: str) -> dcc.Markdown:
    return dcc.Markdown(
        (HELP_DIR / name).read_text(encoding="utf-8"), className="rdm-help", link_target="_blank"
    )


def render(ctx_: AppContext) -> dmc.Stack:
    tabs = [
        dmc.TabsTab("Using the app", value="using", leftSection=icon("tabler:book")),
        dmc.TabsTab("Creating forms", value="forms", leftSection=icon("tabler:table-plus")),
    ]
    panels = [
        dmc.TabsPanel(_md("using_the_app.md"), value="using", pt="md"),
        dmc.TabsPanel(_md("creating_forms.md"), value="forms", pt="md"),
    ]
    if ctx_.permissions.is_global_admin:
        tabs.append(dmc.TabsTab("Administration", value="admin", leftSection=icon("tabler:server-cog")))
        panels.append(dmc.TabsPanel(_md("administration.md"), value="admin", pt="md"))
    subtitle = "How to find, edit and create forms" + (
        ", and how the app maps onto Databricks (administration tab)."
        if ctx_.permissions.is_global_admin
        else "."
    )
    return dmc.Stack(
        [
            page_title("Help", subtitle),
            dmc.Paper(
                dmc.Tabs([dmc.TabsList(tabs), *panels], value="using", id=ids.HELP_TABS),
                withBorder=True,
                p="md",
                radius="md",
            ),
        ],
        gap="md",
    )
