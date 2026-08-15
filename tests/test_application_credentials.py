"""Test region-aware OAuth implementation wiring."""

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.application_credentials import (
    async_get_auth_implementation,
)
from custom_components.ttlock.const import CONF_REGION, DOMAIN
from homeassistant.components.application_credentials import ClientCredential
from homeassistant.core import HomeAssistant

CREDENTIAL = ClientCredential("client-id", "client-secret")
EU_TOKEN = "https://euapi.ttlock.com/oauth2/token"
CN_TOKEN = "https://cnapi.ttlock.com/oauth2/token"


def _entry(hass: HomeAssistant, **data) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"auth_implementation": "mocked", **data}
    )
    entry.add_to_hass(hass)
    return entry


async def test_token_url_follows_china_region(hass: HomeAssistant):
    """A config entry stored with the China region yields the cnapi token URL."""
    _entry(hass, **{CONF_REGION: "cn"})

    impl = await async_get_auth_implementation(hass, "mocked", CREDENTIAL)

    assert impl.token_url == CN_TOKEN


async def test_legacy_entry_without_region_defaults_to_eu(hass: HomeAssistant):
    """Entries created before region support (no region key) stay on euapi."""
    _entry(hass)

    impl = await async_get_auth_implementation(hass, "mocked", CREDENTIAL)

    assert impl.token_url == EU_TOKEN


async def test_no_matching_entry_defaults_to_eu(hass: HomeAssistant):
    """A credential with no config entry yet (mid-flight) defaults to euapi."""
    impl = await async_get_auth_implementation(hass, "orphan", CREDENTIAL)

    assert impl.token_url == EU_TOKEN
