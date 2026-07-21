"""Test ttlock setup process."""

from unittest.mock import patch

from custom_components.ttlock.const import DOMAIN, TT_LOCKS
from custom_components.ttlock.models import LockSummary
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.network import NoURLAvailableError


async def test_setup_unload_and_reload_entry(hass, component_setup, mock_api_responses):
    """Test entry setup and unload."""
    mock_api_responses("default")
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
async def test_no_url(hass, component_setup, mock_api_responses):
    mock_api_responses("default")
    with patch("homeassistant.helpers.issue_registry.async_create_issue") as mock:
        assert await component_setup()
        assert mock.assert_called


async def test_setup_with_non_connectable_lock(
    hass, component_setup, mock_api_responses, monkeypatch, caplog
):
    """Non-connectable locks still get entities, marked unavailable with a reason."""
    mock_api_responses("default")

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=7252408,
                lockAlias="Connectable Lock",
                lockMac="00:00:00:00:00:01",
                hasGateway=1,
            ),
            LockSummary(
                lockId=2,
                lockAlias="No Gateway Lock",
                lockMac="00:00:00:00:00:02",
                hasGateway=0,
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )

    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    coordinators = {
        coordinator.lock_id: coordinator
        for coordinator in hass.data[DOMAIN][entry.entry_id][TT_LOCKS]
    }
    assert coordinators[7252408].connectable is True
    assert coordinators[2].connectable is False

    connectable_entity = next(iter(coordinators[7252408].entities))
    unavailable_entity = next(iter(coordinators[2].entities))

    assert connectable_entity.available is True
    assert unavailable_entity.available is False
    assert "unavailable_reason" in unavailable_entity.extra_state_attributes

    assert "1 lock(s) have no gateway or WiFi" in caplog.text
