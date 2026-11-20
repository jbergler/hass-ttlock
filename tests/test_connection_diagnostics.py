"""Tests for the connection diagnostics.

The diagnostic sensors' states (connection / wifi-local link / cloud online /
bluetooth reachable), driven off mocked coordinator and proxy state.
"""

from __future__ import annotations

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.binary_sensor import LockBleReachable, LockCloudOnline
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    DOMAIN,
    TRANSPORT_CLOUD,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import (
    LockSummary,
)
from custom_components.ttlock.sensor import (
    LockConnection,
    LockWifiLocalLink,
)
from custom_components.ttlock.store import LockStateStore
from homeassistant.util import dt as dt_util


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


class _FakeProxy:
    """A minimal wifi_proxy stand-in for the connection diagnostics."""

    def __init__(self, *, mode=None, reconnecting=False, last_seen=None):
        self._mode = mode
        self._reconnecting = reconnecting
        self._last_seen = last_seen

    def session_mode(self, lock_id):
        return self._mode

    def is_reconnecting_client(self, lock_id):
        return self._reconnecting

    def is_takeover_ready(self, lock_id):
        return self._mode == "takeover"

    def last_seen(self, lock_id):
        return self._last_seen


def _install_proxy(hass, proxy) -> None:
    hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy


# --- connection sensors ------------------------------------------------------


async def test_connection_sensor_reports_cloud_when_only_cloud_reachable(hass, api):
    coordinator = _coordinator(hass, api)
    sensor = LockConnection(coordinator)

    assert sensor.native_value == TRANSPORT_CLOUD
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["cloud_free"] is False
    assert attrs["cloud_enabled"] is True
    assert attrs["wifi_local"] == "down"
    assert sensor.available is True


async def test_connection_sensor_passthrough_is_not_cloud_free(hass, api):
    # A pass-through session relays to the cloud, so control is NOT cloud-free.
    coordinator = _coordinator(hass, api)
    _install_proxy(hass, _FakeProxy(mode="passthrough"))
    sensor = LockConnection(coordinator)
    sensor._update_from_coordinator()

    assert sensor.native_value == TRANSPORT_CLOUD
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["wifi_local"] == "passthrough"
    assert attrs["cloud_free"] is False


async def test_connection_sensor_available_even_when_unreachable(hass, api):
    coordinator = _coordinator(hass, api)
    coordinator._cloud_enabled_option = False  # no cloud, no ble, no wifi
    sensor = LockConnection(coordinator)
    sensor._update_from_coordinator()

    assert sensor.native_value is None  # nothing can reach it
    assert sensor.available is True  # still reports, to show the absence


@pytest.mark.parametrize(
    ("mode", "reconnecting", "expected"),
    [
        (None, False, "down"),
        ("passthrough", False, "passthrough"),
        ("takeover", False, "takeover"),
        (None, True, "reconnecting"),
    ],
)
async def test_wifi_local_link_states(hass, api, mode, reconnecting, expected):
    coordinator = _coordinator(hass, api)
    _install_proxy(hass, _FakeProxy(mode=mode, reconnecting=reconnecting))
    sensor = LockWifiLocalLink(coordinator)
    sensor._update_from_coordinator()

    assert sensor.native_value == expected
    assert sensor._attr_options is not None
    assert expected in sensor._attr_options


async def test_wifi_local_link_reports_when_last_seen(hass, api):
    seen = dt_util.utcnow()
    coordinator = _coordinator(hass, api)
    _install_proxy(hass, _FakeProxy(mode="takeover", last_seen=seen))
    sensor = LockWifiLocalLink(coordinator)

    assert sensor.extra_state_attributes == {"last_seen": seen.isoformat()}


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
