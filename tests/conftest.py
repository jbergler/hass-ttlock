"""Global fixtures for ttlock integration."""

import sys
from time import time
import types
from typing import NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.api import TTLockApi
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import DOMAIN, TT_LOCKS
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import (
    Lock,
    LockRecord,
    LockState,
    LockSummary,
    PassageModeConfig,
    Sensor,
)
from custom_components.ttlock.store import LockStateStore
from homeassistant import components as ha_components
from homeassistant.components.application_credentials import (
    ClientCredential,
    async_import_client_credential,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
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
        await hass.async_block_till_done(wait_background_tasks=True)

        return hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS][0]

    return _setup_func


@pytest.fixture
def multi_account_credential():
    """Factory: register the one developer application ("client-id") that
    new_mocked_entry's config entries all authenticate against, so tests can
    build several config entries sharing a client_id.
    """

    async def _register(hass: HomeAssistant) -> None:
        assert await async_setup_component(hass, "application_credentials", {})
        await async_import_client_credential(
            hass, DOMAIN, ClientCredential("client-id", "client-secret"), "mocked"
        )

    return _register


@pytest.fixture
def new_mocked_entry():
    """Factory: a config entry authenticated via the "mocked" credential
    above - call once per account when a test needs several config entries
    sharing one client_id.
    """

    def _new_entry(**extra_data) -> MockConfigEntry:
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
                **extra_data,
            },
        )

    return _new_entry


@pytest.fixture
async def api():
    """TTLockApi instance for use in tests."""
    async with ClientSession() as session:
        yield TTLockApi(session, None)  # ty: ignore[invalid-argument-type] - unused by the tests that consume this fixture


@pytest.fixture
async def coordinator(hass, api):
    """Co-ordinator instance for use in tests."""
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=7252408, lockAlias="Test Lock", lockMac="00:00:00:00:00:00", hasGateway=1
    )
    return LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
    )


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
            return [
                LockSummary(
                    lockId=mock_data.lock.id,
                    lockAlias=mock_data.lock.name,
                    lockMac=mock_data.lock.mac,
                    featureValue=mock_data.lock.featureValue,
                    hasGateway=1,
                )
            ]

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

        async def mock_get_gateways_for_lock(*args, **kwargs):
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
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_gateways_for_lock",
            mock_get_gateways_for_lock,
        )

    return create_mock_responses


class CloudNotAvailable(HomeAssistantError):
    """Stand-in for homeassistant.components.cloud.CloudNotAvailable."""


class CloudNotConnected(CloudNotAvailable):
    """Stand-in for homeassistant.components.cloud.CloudNotConnected."""


@pytest.fixture(autouse=True)
def mock_cloud(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Stub homeassistant.components.cloud so try_generate_cloudhook actually runs.

    homeassistant.components.cloud is unimportable in this test environment
    (its import chain drags in camera/turbojpeg, then conversation/hassil),
    so webhook.py's own deferred import can't be satisfied by the real
    module. Defaults to no active subscription - the non-cloud path every
    existing test exercised under the old fixture that patched
    try_generate_cloudhook directly. Tests wanting the cloud path override
    stub.async_active_subscription / async_get_or_create_cloudhook.
    """
    stub = types.ModuleType("homeassistant.components.cloud")
    stub.CloudNotAvailable = CloudNotAvailable  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.CloudNotConnected = CloudNotConnected  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.async_active_subscription = MagicMock(return_value=False)  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.async_get_or_create_cloudhook = AsyncMock()  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub

    monkeypatch.setitem(sys.modules, "homeassistant.components.cloud", stub)
    monkeypatch.setattr(ha_components, "cloud", stub, raising=False)

    return stub
