"""Settings from the environment."""

from rdm.config import Settings


def test_admin_contact_from_env_is_optional_and_stripped():
    assert Settings.from_env({}).admin_contact == ""


def test_debug_personas_defaults_off_and_parses_truthy_values():
    assert Settings.from_env({}).debug_personas is False
    assert Settings.from_env({"RDM_DEBUG_PERSONAS": "true"}).debug_personas is True
    assert Settings.from_env({"RDM_DEBUG_PERSONAS": "1"}).debug_personas is True
    assert Settings.from_env({"RDM_DEBUG_PERSONAS": "off"}).debug_personas is False
    assert Settings.from_env({"RDM_ADMIN_CONTACT": "  data-office@example.org "}).admin_contact == (
        "data-office@example.org"
    )
