"""Global fixtures for ttlock integration."""

from time import time
from unittest.mock import patch

from aiohttp import ClientSession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.api import TTLockApi
from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import Lock, LockState, PassageModeConfig
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .const import LOCK_DETAILS, LOCK_STATE_UNLOCKED, PASSAGE_MODE_6_TO_6_7_DAYS

pytest_plugins = "pytest_homeassistant_custom_component"


# This fixture enables loading custom integrations in all tests.
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    yield


# persistent_notification doesn't exist during tests, patch it so we don't get stuck
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with patch("homeassistant.components.persistent_notification.async_create"), patch(
        "homeassistant.components.persistent_notification.async_dismiss"
    ):
        yield


@pytest.fixture
def config_entry():
    """Mock a config entry."""
    mock_entry = MockConfigEntry(
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
    return mock_entry


@pytest.fixture
def component_setup(hass: HomeAssistant, config_entry: MockConfigEntry):
    """Fixture for setting up the integration."""

    async def _setup_func() -> bool:
        assert await async_setup_component(hass, "application_credentials", {})
        await async_import_client_credential(
            hass,
            DOMAIN,
            ClientCredential("client-id", "client-secret"),
            config_entry.data["auth_implementation"],
        )
        config_entry.add_to_hass(hass)
        return await hass.config_entries.async_setup(config_entry.entry_id)

    return _setup_func


@pytest.fixture
async def api():
    """TTLockApi instance for use in tests."""
    session = ClientSession()
    return TTLockApi(session, None)


@pytest.fixture
async def coordinator(hass, api):
    """Co-ordinator instance for use in tests."""
    return LockUpdateCoordinator(hass, api, 7252408)


@pytest.fixture
def sane_default_data():
    """Fixture for mocking sane default data from the API."""

    lock = Lock.parse_obj(LOCK_DETAILS)
    state = LockState.parse_obj(LOCK_STATE_UNLOCKED)
    passage_mode_config = PassageModeConfig.parse_obj(PASSAGE_MODE_6_TO_6_7_DAYS)

    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks", return_value=[lock.id]
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock", return_value=lock
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock_state", return_value=state
    ), patch(
        "custom_components.ttlock.api.TTLockApi.get_lock_passage_mode_config",
        return_value=passage_mode_config,
    ):
        yield
