"""Tests for the lock's admin switches.

Auto-lock and lock-sound are set through the cloud, so an admin gets them while
the cloud is on, and nobody gets them while it is off.
"""

from custom_components.ttlock.const import CONF_CLOUD_ENABLED
from homeassistant.config_entries import ConfigEntryState

from .const import BASIC_EKEY_RECORD_MEMBER


def _switch_names(hass) -> list[str]:
    return [state.name for state in hass.states.async_all("switch")]


class TestSwitchPlatformSetup:
    async def test_admin_switches_with_the_cloud_on(
        self, hass, component_setup, mock_api_responses
    ):
        mock_api_responses("default")
        await component_setup()
        names = _switch_names(hass)
        assert any("Auto Lock" in name for name in names)
        assert any("Lock Sound" in name for name in names)

    async def test_no_switches_with_the_cloud_off(
        self, hass, multi_account_credential, new_mocked_entry, mock_api_responses
    ):
        mock_api_responses("default")
        await multi_account_credential(hass)
        entry = new_mocked_entry(options={CONF_CLOUD_ENABLED: False})
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
        assert entry.state is ConfigEntryState.LOADED
        assert _switch_names(hass) == []

    async def test_no_switches_for_a_shared_key(self, hass, ekey_component_setup):
        await ekey_component_setup(BASIC_EKEY_RECORD_MEMBER)
        assert _switch_names(hass) == []
