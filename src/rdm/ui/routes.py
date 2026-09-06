"""URL routes: the addresses the app links to and how a pathname is resolved back to a page."""

from __future__ import annotations

from urllib.parse import quote, unquote

DOMAINS_HREF = "/domains"
NEW_FUNCTION_HREF = "/new-function"
NEW_FORM_HREF = "/new-form"
NEW_FILE_HREF = "/new-file"


def form_href(function: str, form: str) -> str:
    return f"/f/{quote(function)}/{quote(form)}"


def function_href(function: str) -> str:
    return f"/fn/{quote(function)}"


def file_href(function: str, name: str) -> str:
    return f"/file/{quote(function)}/{quote(name)}"


def domain_href(domain: str) -> str:
    return f"/dm/{quote(domain)}"


def new_form_href(function: str | None = None) -> str:
    """The form wizard, with the function pre-selected when given."""
    return f"{NEW_FORM_HREF}/{quote(function)}" if function else NEW_FORM_HREF


def parse_path(pathname: str | None) -> tuple[str, str | None, str | None]:
    """``/`` -> home, ``/dm/<domain>``, ``/fn/<function>``, ``/f/<function>/<form>``,
    ``/file/<function>/<file>``, ``/new-form[/<function>]``, ``/new-file[/<function>]``,
    ``/new-function``, ``/domains``, ``/help``. Returns ``(view, function-or-domain, object)``."""
    parts = [unquote(p) for p in (pathname or "/").split("/") if p]
    if not parts:
        return "home", None, None
    if parts[0] == "f" and len(parts) >= 3:
        return "form", parts[1], parts[2]
    if parts[0] == "file" and len(parts) >= 3:
        return "file", parts[1], parts[2]
    if parts[0] == "fn" and len(parts) >= 2:
        return "function", parts[1], None
    if parts[0] == "dm" and len(parts) >= 2:
        return "domain", parts[1], None
    if parts[0] == "new-form":
        return "new-form", parts[1] if len(parts) >= 2 else None, None
    if parts[0] == "new-file":
        return "new-file", parts[1] if len(parts) >= 2 else None, None
    if parts[0] == "new-function":
        return "new-function", None, None
    if parts[0] == "domains":
        return "domains", None, None
    if parts[0] == "help":
        return "help", None, None
    return "home", None, None
