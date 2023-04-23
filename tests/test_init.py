"""Test ttlock setup process."""

from custom_components.ttlock.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState


async def test_setup_unload_and_reload_entry(hass, component_setup, sane_default_data):
    """Test entry setup and unload."""
    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED
