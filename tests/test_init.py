"""Test ttlock setup process."""

import logging
from time import time
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.capture import DebugCaptureHandler
from custom_components.ttlock.const import DOMAIN, TT_CAPTURE, TT_LOCKS
from custom_components.ttlock.models import LockSummary
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.setup import async_setup_component


async def test_setup_unload_and_reload_entry(hass, component_setup, mock_api_responses):
    """Test entry setup and unload."""
    mock_api_responses("default")
    await component_setup()

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.state is ConfigEntryState.LOADED

    package_logger = logging.getLogger("custom_components.ttlock")
    assert any(
        isinstance(handler, DebugCaptureHandler) for handler in package_logger.handlers
    )

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state == ConfigEntryState.NOT_LOADED

    # the last entry unloading must detach the shared debug-capture handler
    assert not any(
        isinstance(handler, DebugCaptureHandler) for handler in package_logger.handlers
    )


async def test_capture_handler_is_shared_across_config_entries(
    hass, mock_api_responses
):
    """A single DebugCaptureHandler is attached once, even with two TTLock accounts."""
    mock_api_responses("default")

    assert await async_setup_component(hass, "application_credentials", {})
    await async_import_client_credential(
        hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
    )

    def _new_entry() -> MockConfigEntry:
        return MockConfigEntry(
            domain=DOMAIN,
            data={
                "auth_implementation": "mocked",
                "token": {
                    "refresh_token": "mock-refresh-token",
                    "access_token": "mock-access-token",
                    "type": "Bearer",
                    "expires_in": 60,
                    "expires_at": time() + 1000,
                    "scope": "",
                },
            },
        )

    # entry_b must not be added until entry_a's setup has finished: adding
    # both before any setup call would let HA's component-level bootstrap
    # (triggered by entry_a's setup, the domain's first) auto-forward-setup
    # entry_b too, racing the explicit call below.
    entry_a = _new_entry()
    entry_a.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_a.entry_id)

    entry_b = _new_entry()
    entry_b.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry_b.entry_id)

    await hass.async_block_till_done(wait_background_tasks=True)

    package_logger = logging.getLogger("custom_components.ttlock")
    handlers = [
        handler
        for handler in package_logger.handlers
        if isinstance(handler, DebugCaptureHandler)
    ]
    assert len(handlers) == 1
    assert (
        hass.data[DOMAIN][entry_a.entry_id][TT_CAPTURE]
        is hass.data[DOMAIN][entry_b.entry_id][TT_CAPTURE]
    )

    assert await hass.config_entries.async_unload(entry_a.entry_id)
    assert any(isinstance(h, DebugCaptureHandler) for h in package_logger.handlers)

    assert await hass.config_entries.async_unload(entry_b.entry_id)
    assert not any(isinstance(h, DebugCaptureHandler) for h in package_logger.handlers)


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
