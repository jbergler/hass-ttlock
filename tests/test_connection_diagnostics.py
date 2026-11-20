"""Tests for the connection diagnostics.

The diagnostic sensors' states (connection / cloud online / bluetooth
reachable), driven off mocked coordinator state.
"""

from __future__ import annotations

from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.binary_sensor import LockBleReachable, LockCloudOnline
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    DOMAIN,
    TRANSPORT_CLOUD,
)
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import (
    LockSummary,
)
from custom_components.ttlock.sensor import (
    LockConnection,
)
from custom_components.ttlock.store import LockStateStore


def _coordinator(hass, api, *, mac="16:72:4C:CC:01:C4", feature_value=None):
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=7252408,
        lockAlias="Front Door",
        lockMac=mac,
        hasGateway=1,
        featureValue=feature_value,
        userType="110301",
    )
    return LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
    )


# --- connection sensors ------------------------------------------------------


async def test_connection_sensor_reports_cloud_when_only_cloud_reachable(hass, api):
    coordinator = _coordinator(hass, api)
    sensor = LockConnection(coordinator)

    assert sensor.native_value == TRANSPORT_CLOUD
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["cloud_free"] is False
    assert sensor.available is True


async def test_connection_sensor_available_even_when_unreachable(hass, api):
    coordinator = _coordinator(hass, api)
    coordinator.cloud_connectable = False  # no cloud, no ble
    sensor = LockConnection(coordinator)
    sensor._update_from_coordinator()

    assert sensor.native_value is None  # nothing can reach it
    assert sensor.available is True  # still reports, to show the absence


async def test_cloud_online_binary_sensor_off_without_gateway_coordinator(hass, api):
    coordinator = _coordinator(hass, api)
    sensor = LockCloudOnline(coordinator)

    # Cloud enabled + connectable, but no live gateway coordinator -> not online.
    assert sensor.is_on is False
    assert sensor.available is True


async def test_ble_reachable_binary_sensor_reports_radio_and_rssi(
    hass, api, monkeypatch
):
    coordinator = _coordinator(hass, api)
    monkeypatch.setattr(type(coordinator), "ble_reachable", property(lambda self: True))
    coordinator.ble.rssi = -57  # BleData.in_range derives from rssi being set
    sensor = LockBleReachable(coordinator)
    sensor._update_from_coordinator()

    assert sensor.is_on is True
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["rssi"] == -57
    assert sensor.available is True
