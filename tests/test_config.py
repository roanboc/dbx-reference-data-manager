"""Settings from the environment."""

from rdm.config import Settings


def test_admin_contact_from_env_is_optional_and_stripped():
    assert Settings.from_env({}).admin_contact == ""
    assert Settings.from_env({"RDM_ADMIN_CONTACT": "  data-office@example.org "}).admin_contact == (
        "data-office@example.org"
    )
