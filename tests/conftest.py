"""Global fixtures for ttlock integration."""

from collections.abc import Generator
from time import time
from typing import NamedTuple
from unittest.mock import patch

from aiohttp import ClientSession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.api import TTLockApi
from custom_components.ttlock.const import DOMAIN, TT_LOCKS
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import (
    Lock,
    LockRecord,
    LockState,
    PassageModeConfig,
    Sensor,
)
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component

from .const import (
    BASIC_LOCK_DETAILS,
    LOCK_DETAILS_WITH_SENSOR,
    LOCK_STATE_LOCKED,
    LOCK_STATE_UNLOCKED,
    PASSAGE_MODE_6_TO_6_7_DAYS,
    SENSOR_DETAILS,
)

pytest_plugins = "pytest_homeassistant_custom_component"


# This fixture enables loading custom integrations in all tests.
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return enable_custom_integrations


# persistent_notification doesn't exist during tests, patch it so we don't get stuck
@pytest.fixture(name="skip_notifications", autouse=True)
def skip_notifications_fixture():
    """Skip notification calls."""
    with (
        patch("homeassistant.components.persistent_notification.async_create"),
        patch("homeassistant.components.persistent_notification.async_dismiss"),
    ):
        yield


@pytest.fixture
def config_entry():
    """Mock a config entry."""
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
        await hass.config_entries.async_setup(config_entry.entry_id)

        return hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS][0]

    return _setup_func


@pytest.fixture
async def api():
    """TTLockApi instance for use in tests."""
    session = ClientSession()
    return TTLockApi(session, None)  # ty: ignore[invalid-argument-type] - unused by the tests that consume this fixture


@pytest.fixture
async def coordinator(hass, api):
    """Co-ordinator instance for use in tests."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    return LockUpdateCoordinator(hass, config_entry, api, 7252408)


class MockApiData(NamedTuple):
    """Container for mock API response data."""

    lock: Lock
    state: LockState
    sensor: Sensor | None = None
    passage_mode: PassageModeConfig | None = None
    records: tuple[LockRecord, ...] = ()


@pytest.fixture
def mock_data_factory():
    """Factory fixture to create different sets of mock data."""

    def create_mock_data(scenario: str = "default") -> MockApiData:
        scenarios = {
            "default": MockApiData(
                lock=Lock.model_validate(BASIC_LOCK_DETAILS),
                state=LockState.model_validate(LOCK_STATE_UNLOCKED),
                passage_mode=PassageModeConfig.model_validate(
                    PASSAGE_MODE_6_TO_6_7_DAYS
                ),
            ),
            "with_sensor": MockApiData(
                lock=Lock.model_validate(LOCK_DETAILS_WITH_SENSOR),
                sensor=Sensor.model_validate(SENSOR_DETAILS),
                state=LockState.model_validate(LOCK_STATE_UNLOCKED),
            ),
            "sensor_not_installed": MockApiData(
                lock=Lock.model_validate(LOCK_DETAILS_WITH_SENSOR),
                state=LockState.model_validate(LOCK_STATE_UNLOCKED),
            ),
            "locked": MockApiData(
                lock=Lock.model_validate(BASIC_LOCK_DETAILS),
                state=LockState.model_validate(LOCK_STATE_LOCKED),
                passage_mode=PassageModeConfig.model_validate(
                    PASSAGE_MODE_6_TO_6_7_DAYS
                ),
            ),
            "no_passage_mode": MockApiData(
                lock=Lock.model_validate(BASIC_LOCK_DETAILS),
                state=LockState.model_validate(LOCK_STATE_UNLOCKED),
                passage_mode=None,
            ),
        }
        return scenarios[scenario]

    return create_mock_data


@pytest.fixture
def mock_api_responses(monkeypatch, mock_data_factory):
    """Fixture for mocking TTLock API responses with configurable data."""

    def create_mock_responses(scenario: str = "default"):
        mock_data = mock_data_factory(scenario)

        async def mock_get_locks(*args, **kwargs):
            return [mock_data.lock.id]

        async def mock_get_lock(*args, **kwargs):
            return mock_data.lock

        async def mock_get_sensor(*args, **kwargs):
            return mock_data.sensor

        async def mock_get_lock_state(*args, **kwargs):
            return mock_data.state

        async def mock_get_passage_mode(*args, **kwargs):
            return mock_data.passage_mode

        async def mock_get_lock_records(*args, **kwargs):
            return mock_data.records

        async def mock_get_gateways(*args, **kwargs):
            return []

        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock", mock_get_lock
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_sensor", mock_get_sensor
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_state", mock_get_lock_state
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_passage_mode_config",
            mock_get_passage_mode,
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_records",
            mock_get_lock_records,
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_gateways",
            mock_get_gateways,
        )

    return create_mock_responses


@pytest.fixture(autouse=True)
def mock_webhook_cloudhook() -> Generator[None]:
    """Fixture to mock home assistant cloud."""
    with (
        patch(
            "custom_components.ttlock.webhook.WebhookHandler.try_generate_cloudhook",
            return_value=None,
        ),
    ):
        yield
