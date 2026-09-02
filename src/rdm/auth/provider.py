"""Authentication providers.

* :class:`MockAuthProvider` - three local personas (Administrator, Editor, Viewer) selected in
  the sidebar or through ``RDM_PERSONA``.
* :class:`DatabricksAuthProvider` - reads the identity headers Databricks Apps injects
  (``X-Forwarded-Email``, ``X-Forwarded-Preferred-Username``, ``X-Forwarded-User``) and,
  when user authorization is enabled, the user's access token
  (``X-Forwarded-Access-Token``) so that SQL runs on behalf of the user.

Authorisation (which role a user has on a domain) is *not* decided here: the backend
resolves it from grants (``DatabaseBackend.get_permissions``).
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass

from rdm.models import User

log = logging.getLogger(__name__)

HEADER_EMAIL = "X-Forwarded-Email"
HEADER_USERNAME = "X-Forwarded-Preferred-Username"
HEADER_USER_ID = "X-Forwarded-User"
HEADER_ACCESS_TOKEN = "X-Forwarded-Access-Token"


@dataclass(frozen=True)
class Persona:
    key: str
    user: User
    description: str

    @property
    def label(self) -> str:
        return self.user.display_name


#: Local personas. Their groups are referenced by the demo grants in ``scripts/seed_demo.py``.
PERSONAS: dict[str, Persona] = {
    "admin": Persona(
        "admin",
        User(
            username="alice.admin@example.org",
            display_name="Alice Admin",
            email="alice.admin@example.org",
            groups=("rdm_admins", "everyone"),
        ),
        "Administrator - creates forms, edits schemas and data in every domain",
    ),
    "editor": Persona(
        "editor",
        User(
            username="eddie.editor@example.org",
            display_name="Eddie Editor",
            email="eddie.editor@example.org",
            groups=("student_stewards", "finance_readers", "everyone"),
        ),
        "Editor - edits rows in the Student domain, reads Finance, no HR access",
    ),
    "viewer": Persona(
        "viewer",
        User(
            username="vera.viewer@example.org",
            display_name="Vera Viewer",
            email="vera.viewer@example.org",
            groups=("student_readers", "hr_readers", "everyone"),
        ),
        "Viewer - read-only access to Student and HR reference lists",
    ),
}


class AuthProvider(ABC):
    """Resolves the signed-in user for the current Streamlit session."""

    name: str = "abstract"

    @abstractmethod
    def current_user(self, persona: str | None = None) -> User:
        """Return the current user. ``persona`` is only meaningful for the mock provider."""

    @property
    def supports_persona_switching(self) -> bool:
        return False

    def personas(self) -> list[Persona]:
        return []

    def access_token(self) -> str | None:
        """User access token for on-behalf-of-user SQL execution, if available."""
        return None


class MockAuthProvider(AuthProvider):
    name = "mock"

    def __init__(self, default_persona: str = "admin") -> None:
        self.default_persona = default_persona if default_persona in PERSONAS else "admin"

    def current_user(self, persona: str | None = None) -> User:
        key = persona if persona in PERSONAS else self.default_persona
        return PERSONAS[key].user

    @property
    def supports_persona_switching(self) -> bool:
        return True

    def personas(self) -> list[Persona]:
        return list(PERSONAS.values())


class DatabricksAuthProvider(AuthProvider):
    """Identity from the Databricks Apps reverse proxy headers.

    ``headers`` is injected for testability; in the app it is ``st.context.headers``.
    Group membership is fetched with the Databricks SDK using the user's token when user
    authorization is enabled (default scope ``iam.current-user:read``), otherwise with the
    app service principal (needs permission to read users). Results are cached briefly.
    """

    name = "databricks"

    def __init__(self, headers_getter, group_cache_ttl: int = 300) -> None:
        self._headers_getter = headers_getter
        self._group_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._ttl = group_cache_ttl

    def _headers(self) -> Mapping[str, str]:
        try:
            headers = self._headers_getter()
        except Exception:  # pragma: no cover - only outside a Streamlit session
            log.exception("Could not read request headers")
            return {}
        return headers or {}

    def access_token(self) -> str | None:
        token = self._headers().get(HEADER_ACCESS_TOKEN)
        return token or None

    def current_user(self, persona: str | None = None) -> User:
        h = self._headers()
        email = h.get(HEADER_EMAIL) or ""
        username = h.get(HEADER_USERNAME) or email
        if not username:
            raise RuntimeError(
                "No user identity headers found. Run behind Databricks Apps or use "
                "`databricks apps run-local`, or set RDM_AUTH=mock for local development."
            )
        groups = self._groups_for(username, self.access_token())
        return User(username=username, display_name=username, email=email or None, groups=groups)

    def _groups_for(self, username: str, token: str | None) -> tuple[str, ...]:
        now = time.monotonic()
        cached = self._group_cache.get(username)
        if cached and cached[0] > now:
            return cached[1]
        groups: tuple[str, ...] = ()
        try:
            from databricks.sdk import WorkspaceClient

            if token:
                me = WorkspaceClient(token=token, auth_type="pat").current_user.me()
                groups = tuple(g.display for g in (me.groups or []) if g.display)
            else:
                w = WorkspaceClient()
                users = list(w.users.list(filter=f"userName eq '{username}'", attributes="groups"))
                if users:
                    groups = tuple(g.display for g in (users[0].groups or []) if g.display)
        except Exception:  # noqa: BLE001 - group lookup is best effort; UC still enforces access
            log.exception("Group lookup failed for %s", username)
        self._group_cache[username] = (now + self._ttl, groups)
        return groups
