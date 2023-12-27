"""Test ttlock setup process."""

from unittest.mock import patch

from custom_components.ttlock.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.network import NoURLAvailableError


async def test_setup_unload_and_reload_entry(hass, component_setup, sane_default_data):
    """Test entry setup and unload."""
    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED


@patch(
    "homeassistant.components.webhook.async_generate_url",
    side_effect=NoURLAvailableError,
)
async def test_no_url(hass, component_setup, sane_default_data):
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock:
        assert await component_setup()
        assert mock.assert_called
