"""Test the sensor platform's entity setup.

See coordinator.py's async_add_when_sensor_present: door-sensor presence
isn't known at platform-setup time - it's filled in by each lock's first
refresh, which __init__.py runs in the background after platforms are set up
(_fill_lock_details) - so SensorBattery has to be added once that refresh
confirms presence, not decided up front.
"""

from unittest.mock import AsyncMock, patch

from custom_components.ttlock.const import (
    CONF_BLUETOOTH_ENABLED,
    CONF_WIFI_LOCAL_ENABLED,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TT_LOCKS,
)
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import GatewayLink, LockSummary
from custom_components.ttlock.sensor import LockBleSignal, LockConnection
from homeassistant.helpers import entity_registry as er

from .const import BASIC_EKEY_RECORD_MEMBER, MOCK_LOCK_MAC


def _registered(hass, coordinator, suffix: str) -> bool:
    return (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{coordinator.unique_id}-{suffix}"
        )
        is not None
    )


async def test_wifi_local_link_sensor_needs_the_wifi_proxy(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("default")
    coordinator = await component_setup()
    assert not _registered(hass, coordinator, "lockwifilocallink")


async def _wifi_local_entry(hass, new_mocked_entry, *, wifi_lock: bool, **options):
    lock = LockSummary(
        lockId=7252408,
        lockAlias="Front Door",
        lockMac=MOCK_LOCK_MAC,
        featureValue=f"{2**56:X}" if wifi_lock else "F44354CD5F3",
        userType="110301",
    )
    entry = new_mocked_entry(options={CONF_WIFI_LOCAL_ENABLED: True, **options})
    entry.add_to_hass(hass)
    with patch(
        "custom_components.ttlock.api.TTLockApi.get_locks",
        AsyncMock(return_value=[lock]),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done(wait_background_tasks=True)
    return hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]


async def test_wifi_local_link_sensor_with_the_wifi_proxy_on(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    coordinator = await _wifi_local_entry(hass, new_mocked_entry, wifi_lock=True)
    assert _registered(hass, coordinator, "lockwifilocallink")


async def test_no_wifi_local_entities_for_a_lock_without_wifi(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses
):
    """The WiFi proxy option alone does nothing for a Bluetooth-only lock."""
    mock_api_responses("default")
    await multi_account_credential(hass)
    coordinator = await _wifi_local_entry(
        hass, new_mocked_entry, wifi_lock=False, cloud_enabled=False
    )
    assert not _registered(hass, coordinator, "lockwifilocallink")
    assert not _registered(hass, coordinator, "lockcredentials")
    registry = er.async_get(hass)
    assert (
        registry.async_get_entity_id(
            "switch", DOMAIN, f"{coordinator.unique_id}-tamperalert"
        )
        is None
    )


async def test_credentials_sensor_for_an_admin_key(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("default")
    coordinator = await component_setup()
    assert _registered(hass, coordinator, "lockcredentials")


async def test_credentials_sensor_with_the_cloud_off_and_a_local_transport(
    hass, component_setup, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: False)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    coordinator = await component_setup()
    assert _registered(hass, coordinator, "lockcredentials")


async def test_no_credentials_sensor_with_nothing_to_fill_it(
    hass, component_setup, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: False)
    )
    coordinator = await component_setup()
    assert not _registered(hass, coordinator, "lockcredentials")


async def test_no_credentials_sensor_for_a_shared_key(hass, ekey_component_setup):
    coordinator = await ekey_component_setup(BASIC_EKEY_RECORD_MEMBER)
    assert not _registered(hass, coordinator, "lockcredentials")


async def test_sensor_battery_entity_appears_once_presence_confirmed(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("with_sensor")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get("sensor.front_door_sensor_battery")
    assert state is not None
    assert state.state == "85"


async def test_sensor_battery_entity_not_created_when_absent(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("sensor_not_installed")
    await component_setup()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get("sensor.front_door_sensor_battery") is None


async def test_lock_battery_entity_created_immediately(
    hass, component_setup, mock_api_responses
):
    """Unlike SensorBattery, LockBattery doesn't depend on the deferred refresh."""
    mock_api_responses("default")
    await component_setup()

    assert hass.states.get("sensor.front_door_battery") is not None


async def test_gateway_signal_entity_disabled_by_default(
    hass, component_setup, mock_api_responses
):
    mock_api_responses("default")
    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockgateway"
    )
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def test_gateway_signal_entity_reports_best_gateway_and_others(
    hass, component_setup, mock_api_responses, monkeypatch
):
    mock_api_responses("default")

    async def mock_get_gateways_for_lock(*args, **kwargs):
        return [
            GatewayLink(
                gatewayId=2,
                gatewayName="Strong",
                gatewayMac="00:00:00:00:00:02",
                rssi=-60,
            ),
            GatewayLink(
                gatewayId=1,
                gatewayName="Weak",
                gatewayMac="00:00:00:00:00:01",
                rssi=-85,
            ),
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_gateways_for_lock",
        mock_get_gateways_for_lock,
    )

    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockgateway"
    )
    assert entity_id is not None
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(coordinator.config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "-60"
    assert state.attributes["gateway"] == "Strong"
    assert state.attributes["mac"] == "00:00:00:00:00:02"
    assert state.attributes["other_gateways"] == [
        {"name": "Weak", "mac": "00:00:00:00:00:01", "rssi": -85}
    ]


async def test_gateway_signal_entity_not_created_for_wifi_only_lock(
    hass, component_setup, mock_api_responses, monkeypatch
):
    mock_api_responses("default")

    async def mock_get_locks(*args, **kwargs):
        return [
            LockSummary(
                lockId=7252408,
                lockAlias="Front Door",
                lockMac="00:00:00:00:00:00",
                hasGateway=0,
                featureValue=f"{2**56:X}",  # Features.wifi
                userType="110301",
            )
        ]

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_locks", mock_get_locks
    )

    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockgateway"
    )
    assert entity_id is None


async def test_bluetooth_signal_entity_not_created_without_bluetooth(
    hass, component_setup, mock_api_responses
):
    """A cloud-only host would only ever get an entity stuck on "unknown"."""
    mock_api_responses("default")
    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockblesignal"
    )
    assert entity_id is None


async def test_bluetooth_signal_entity_not_created_when_bluetooth_is_disabled(
    hass, multi_account_credential, new_mocked_entry, mock_api_responses, mock_bluetooth
):
    mock_api_responses("default")
    await multi_account_credential(hass)
    entry = new_mocked_entry(options={CONF_BLUETOOTH_ENABLED: False})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = hass.data[DOMAIN][entry.entry_id][TT_LOCKS][0]

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockblesignal"
    )
    assert entity_id is None


async def test_bluetooth_signal_entity_disabled_by_default(
    hass, component_setup, mock_api_responses, mock_bluetooth
):
    mock_api_responses("default")
    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockblesignal"
    )
    assert entity_id is not None
    entry = registry.async_get(entity_id)
    assert entry is not None
    assert entry.disabled_by == er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None


async def test_bluetooth_signal_entity_reports_locally_heard_rssi(
    hass, component_setup, mock_api_responses, mock_bluetooth, ble_advertisement
):
    """The whole point of M1: how well does *this host* hear the lock?"""
    mock_api_responses("default")
    mock_bluetooth.seed = ble_advertisement(
        address=MOCK_LOCK_MAC, rssi=-58, source="00:11:22:33:44:55"
    )
    coordinator = await component_setup()

    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockblesignal"
    )
    assert entity_id is not None
    registry.async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(coordinator.config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    state = hass.states.get(entity_id)
    assert state is not None
    assert state.state == "-58"
    assert state.attributes["source"] == "00:11:22:33:44:55"
    assert state.attributes["connectable"] is True
    assert state.attributes["last_seen"] is not None


async def test_bluetooth_signal_entity_has_no_attributes_when_never_heard(coordinator):
    """A lock we've never heard exposes no source/last_seen to report."""
    assert LockBleSignal(coordinator).extra_state_attributes is None


async def test_connection_shows_bluetooth_when_it_reads_the_lock_without_control(
    coordinator, monkeypatch
):
    """The value is the transport that reaches the lock, not the control path."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    sensor = LockConnection(coordinator)

    assert coordinator.ble_control_enabled is False
    assert sensor.native_value == TRANSPORT_BLE
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["preferred_control_transport"] == TRANSPORT_CLOUD


async def test_connection_never_shows_a_disabled_cloud(coordinator):
    coordinator._cloud_enabled_option = False  # noqa: SLF001 - the option itself
    sensor = LockConnection(coordinator)

    assert coordinator.cloud_connectable is True
    assert sensor.native_value is None
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["available_transports"] == []
    assert attrs["preferred_control_transport"] is None
