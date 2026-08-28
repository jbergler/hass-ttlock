"""Test the sensor platform's entity setup.

See coordinator.py's async_add_when_sensor_present: door-sensor presence
isn't known at platform-setup time - it's filled in by each lock's first
refresh, which __init__.py runs in the background after platforms are set up
(_fill_lock_details) - so SensorBattery has to be added once that refresh
confirms presence, not decided up front.
"""

from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.models import GatewayLink, LockSummary
from custom_components.ttlock.sensor import LockBleSignal
from homeassistant.helpers import entity_registry as er

from .const import MOCK_LOCK_MAC


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
