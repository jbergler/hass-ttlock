"""Global fixtures for ttlock integration."""

from collections.abc import Callable
import sys
from time import time
import types
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession
from bleak.backends.device import BLEDevice

# The real objects HA's bluetooth component re-exports, imported from the
# packages it gets them from because the component itself can't be loaded
# here (see mock_bluetooth) - so ble.py still measures advertisement age
# against the same clock and matches the same scanning-mode enum it would in
# production.
from bluetooth_data_tools import monotonic_time_coarse
from habluetooth import BluetoothScanningMode
from home_assistant_bluetooth import BluetoothServiceInfoBleak
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
    MOCK_LOCK_MAC,
    MOCK_LOCK_NAME,
    PASSAGE_MODE_6_TO_6_7_DAYS,
    SENSOR_DETAILS,
    TTLOCK_SERVICE_UUID,
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
                    # opaque binary blob, value doesn't matter to us, just presence
                    lockData="TW9ja0xvY2tEYXRhQmxvYkZvclRlc3Rz",
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
    stub.async_delete_cloudhook = AsyncMock()  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub

    monkeypatch.setitem(sys.modules, "homeassistant.components.cloud", stub)
    monkeypatch.setattr(ha_components, "cloud", stub, raising=False)

    return stub


class BluetoothRecorder:
    """What ble.py asked HA's bluetooth component to do."""

    def __init__(self) -> None:
        self.matcher: dict | None = None
        self.mode: BluetoothScanningMode | None = None
        self.advertisement: Callable | None = None
        self.unavailable: Callable | None = None
        self.unavailable_address: str | None = None
        self.unavailable_connectable: bool | None = None
        self.seed: BluetoothServiceInfoBleak | None = None
        self.seed_address: str | None = None
        self.unsubscribed: list[str] = []


@pytest.fixture
def ble_advertisement():
    """Factory: one plausible advertisement from a TTLock lock."""

    def _advertisement(**overrides) -> BluetoothServiceInfoBleak:
        address = overrides.pop("address", MOCK_LOCK_MAC)
        kwargs: dict[str, Any] = {
            "name": MOCK_LOCK_NAME,
            "address": address,
            "rssi": -62,
            "manufacturer_data": {},
            "service_data": {},
            "service_uuids": [TTLOCK_SERVICE_UUID],
            "source": "local",
            "device": BLEDevice(address, MOCK_LOCK_NAME, {}),
            "advertisement": None,
            "connectable": True,
            "time": monotonic_time_coarse(),
            "tx_power": -127,
        }
        kwargs.update(overrides)
        return BluetoothServiceInfoBleak(**kwargs)

    return _advertisement


@pytest.fixture
def mock_bluetooth(
    monkeypatch: pytest.MonkeyPatch, hass: HomeAssistant
) -> BluetoothRecorder:
    """Stand in for a set-up bluetooth component, recording every call.

    homeassistant.components.bluetooth is unimportable in this test
    environment - its import chain reaches the usb component and then
    aiousbwatcher/asyncinotify, which are Linux-only - so it gets the same
    treatment as mock_cloud above, and for the same reason ble.py defers the
    import rather than taking it at module scope.

    Marking "bluetooth" as a loaded component is part of the stub: that is
    exactly what ble.async_bluetooth_available checks before it will import
    or call anything here, so requesting this fixture is what turns local
    Bluetooth on for a test.
    """
    recorder = BluetoothRecorder()

    def register_callback(hass, callback, matcher, mode):
        recorder.advertisement = callback
        recorder.matcher = matcher
        recorder.mode = mode
        return lambda: recorder.unsubscribed.append("advertisement")

    def track_unavailable(hass, callback, address, connectable):
        recorder.unavailable = callback
        recorder.unavailable_address = address
        recorder.unavailable_connectable = connectable
        return lambda: recorder.unsubscribed.append("unavailable")

    def last_service_info(hass, address, connectable=True):
        recorder.seed_address = address
        return recorder.seed

    stub = types.ModuleType("homeassistant.components.bluetooth")
    stub.async_register_callback = register_callback  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.async_track_unavailable = track_unavailable  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.async_last_service_info = last_service_info  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.BluetoothScanningMode = BluetoothScanningMode  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub
    stub.MONOTONIC_TIME = monotonic_time_coarse  # ty: ignore[unresolved-attribute] - types.ModuleType has no declared attrs, this is a test stub

    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", stub)
    monkeypatch.setattr(ha_components, "bluetooth", stub, raising=False)
    hass.config.components.add("bluetooth")

    return recorder
