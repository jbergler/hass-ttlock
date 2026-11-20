"""Tests for the connection diagnostics, device_info MAC links and credential names.

Covers the three additions:
- the diagnostic sensors' states (connection / wifi-local link / cloud online /
  bluetooth reachable), driven off mocked coordinator and proxy state;
- device_info declaring both the BLE (CONNECTION_BLUETOOTH) and the derived +1
  WiFi (CONNECTION_NETWORK_MAC) connections, and omitting the WiFi one safely;
- the best-effort credential-name fetch building an id -> name/type/validity
  mapping without ever storing the secret digits.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock.binary_sensor import LockBleReachable, LockCloudOnline
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    DOMAIN,
    TRANSPORT_CLOUD,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import (
    CredentialInfo,
    CredentialNames,
    LockUpdateCoordinator,
    _wifi_mac_from_ble,
)
from custom_components.ttlock.models import (
    Card,
    Fingerprint,
    LockSummary,
    Passcode,
)
from custom_components.ttlock.sensor import (
    LockConnection,
    LockCredentials,
    LockWifiLocalLink,
)
from custom_components.ttlock.store import LockStateStore
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

WIFI_FEATURE = f"{2**56:X}"


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


def _enable_credentials_sensor(hass, coordinator) -> None:
    """Enable the (off by default) Credentials sensor the name refresh feeds."""
    er.async_get(hass).async_get_or_create(
        "sensor", DOMAIN, f"{coordinator.unique_id}-lockcredentials"
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


# --- _wifi_mac_from_ble ------------------------------------------------------


@pytest.mark.parametrize(
    ("ble", "expected"),
    [
        ("16:72:4C:CC:01:C4", "16:72:4C:CC:01:C5"),
        ("02:00:5E:10:00:9E", "02:00:5E:10:00:9F"),
        # Last octet FF would wrap to 00 and risk a wrong merge - omit instead.
        ("16:72:4C:CC:01:FF", None),
        ("not-a-mac", None),
        ("16:72:4C:CC:01:ZZ", None),
        ("16:72:4C", None),
        ("", None),
    ],
)
def test_wifi_mac_from_ble(ble, expected):
    assert _wifi_mac_from_ble(ble) == expected


# --- device_info connections -------------------------------------------------


async def test_device_info_declares_both_mac_connections(hass, api):
    coordinator = _coordinator(hass, api, feature_value=WIFI_FEATURE)
    connections = coordinator.device_info["connections"]

    assert (dr.CONNECTION_BLUETOOTH, dr.format_mac("16:72:4C:CC:01:C4")) in connections
    # BLE +1 in the last octet: the derived WiFi client MAC.
    assert (
        dr.CONNECTION_NETWORK_MAC,
        dr.format_mac("16:72:4C:CC:01:C5"),
    ) in connections


async def test_device_info_omits_wifi_connection_for_non_wifi_lock(hass, api):
    coordinator = _coordinator(hass, api, feature_value=None)
    connections = coordinator.device_info["connections"]

    assert (dr.CONNECTION_BLUETOOTH, dr.format_mac("16:72:4C:CC:01:C4")) in connections
    assert not any(c[0] == dr.CONNECTION_NETWORK_MAC for c in connections)


async def test_device_info_omits_wifi_connection_for_malformed_mac(hass, api):
    # A malformed MAC must not crash device_info - just omit the WiFi link.
    coordinator = _coordinator(hass, api, mac="broken", feature_value=WIFI_FEATURE)
    connections = coordinator.device_info["connections"]

    assert not any(c[0] == dr.CONNECTION_NETWORK_MAC for c in connections)


async def test_device_info_links_via_another_integrations_wifi_device(hass, api):
    """A shared connection doesn't merge devices across config entries, so a
    gatewayless WiFi lock links via whichever device holds its WiFi MAC."""
    coordinator = _coordinator(hass, api, feature_value=WIFI_FEATURE)
    registry = dr.async_get(hass)
    wifi_mac = dr.format_mac("16:72:4C:CC:01:C5")

    other_entry = MockConfigEntry(domain="network")
    other_entry.add_to_hass(hass)
    network_device = registry.async_get_or_create(
        config_entry_id=other_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, wifi_mac)},
        name="WiFi client",
    )

    assert coordinator.config_entry is not None
    ttlock_device = registry.async_get_or_create(
        config_entry_id=coordinator.config_entry.entry_id, **coordinator.device_info
    )

    assert ttlock_device.id != network_device.id
    assert ttlock_device.via_device_id == network_device.id
    # But the lock's device does carry both MAC connections.
    assert (dr.CONNECTION_NETWORK_MAC, wifi_mac) in ttlock_device.connections
    assert (
        dr.CONNECTION_BLUETOOTH,
        dr.format_mac("16:72:4C:CC:01:C4"),
    ) in ttlock_device.connections


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


# --- credential names --------------------------------------------------------


async def test_credential_names_built_without_secret_digits(hass, api, monkeypatch):
    coordinator = _coordinator(hass, api)
    _enable_credentials_sensor(hass, coordinator)

    async def passcodes(_lock_id):
        return [
            Passcode(
                keyboardPwdId=11,
                keyboardPwd="1234",  # secret - must never be stored
                keyboardPwdName="Cleaner",
                keyboardPwdType=2,
                startDate=0,  # permanent - must normalise to no window, not 1970
                endDate=0,
            )
        ]

    async def cards(_lock_id):
        return [Card(cardId=21, cardNumber="999888", cardName="Fob", cardType=1)]

    async def fingerprints(_lock_id):
        return [
            Fingerprint(
                fingerprintId=31,
                fingerprintNumber="44668054142981",
                fingerprintName="Thumb",
                fingerprintType=1,
            )
        ]

    monkeypatch.setattr(api, "list_passcodes", passcodes)
    monkeypatch.setattr(api, "list_cards", cards)
    monkeypatch.setattr(api, "list_fingerprints", fingerprints)

    await coordinator._maybe_refresh_credentials(dt_util.now())

    names = coordinator.credential_names
    assert names.counts == {"passcodes": 1, "cards": 1, "fingerprints": 1}
    assert names.passcodes[11] == CredentialInfo(
        name="Cleaner", type="permanent", valid_from=None, valid_until=None
    )
    assert names.cards[21].name == "Fob"
    assert names.cards[21].type == "normal"
    assert names.fingerprints[31].name == "Thumb"

    # The secret digits / numbers must not appear anywhere in the stored map.
    blob = repr(names)
    assert "1234" not in blob
    assert "999888" not in blob
    assert "44668054142981" not in blob

    # And they are surfaced by the diagnostic sensor (names, not digits).
    sensor = LockCredentials(coordinator)
    sensor._update_from_coordinator()
    assert sensor.native_value == 3
    attrs = sensor.extra_state_attributes
    assert attrs is not None
    assert attrs["passcode_count"] == 1
    assert [p["name"] for p in attrs["passcodes"]] == ["Cleaner"]
    assert "1234" not in repr(attrs)


async def test_credential_fetch_is_best_effort_per_kind(hass, api, monkeypatch):
    coordinator = _coordinator(hass, api)
    _enable_credentials_sensor(hass, coordinator)

    async def ok_passcodes(_lock_id):
        return [Passcode(keyboardPwdId=1, keyboardPwdName="A", keyboardPwdType=2)]

    async def boom(_lock_id):
        raise RuntimeError("no fingerprint hardware")

    async def empty(_lock_id):
        return []

    monkeypatch.setattr(api, "list_passcodes", ok_passcodes)
    monkeypatch.setattr(api, "list_cards", empty)
    monkeypatch.setattr(api, "list_fingerprints", boom)

    await coordinator._maybe_refresh_credentials(dt_util.now())

    # One kind failing doesn't blank the others or raise.
    assert coordinator.credential_names.counts["passcodes"] == 1
    assert coordinator.credential_names.counts["fingerprints"] == 0


async def test_credential_fetch_stamps_on_attempt_not_success(hass, api, monkeypatch):
    coordinator = _coordinator(hass, api)
    _enable_credentials_sensor(hass, coordinator)
    calls = {"n": 0}

    async def counting(_lock_id):
        calls["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(api, "list_passcodes", counting)
    monkeypatch.setattr(api, "list_cards", counting)
    monkeypatch.setattr(api, "list_fingerprints", counting)

    now = dt_util.now()
    await coordinator._maybe_refresh_credentials(now)
    first = calls["n"]
    # A second immediate poll is within the slow interval -> no re-fetch.
    await coordinator._maybe_refresh_credentials(now)
    assert calls["n"] == first


async def test_credential_names_persist_and_restore(hass, api, monkeypatch):
    store = LockStateStore(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=7252408,
        lockAlias="Front Door",
        lockMac="16:72:4C:CC:01:C4",
        hasGateway=1,
        userType="110301",
    )
    first = LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), store
    )
    _enable_credentials_sensor(hass, first)

    async def passcodes(_lock_id):
        return [
            Passcode(keyboardPwdId=11, keyboardPwdName="Cleaner", keyboardPwdType=2),
            Passcode(keyboardPwdName="No id", keyboardPwdType=2),  # cannot be keyed
        ]

    async def empty(_lock_id):
        return []

    monkeypatch.setattr(api, "list_passcodes", passcodes)
    monkeypatch.setattr(api, "list_cards", empty)
    monkeypatch.setattr(api, "list_fingerprints", empty)
    await first._maybe_refresh_credentials(dt_util.now())

    # A fresh coordinator (same store) restores the names without any cloud call.
    second = LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), store
    )
    await second.async_restore_credential_names()
    assert second.credential_names.passcodes[11].name == "Cleaner"
    assert list(second.credential_names.passcodes) == [11]

    # Names already in memory are newer than the store's and are kept.
    third = LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), store
    )
    third.credential_names = CredentialNames(cards={4: CredentialInfo(name="Fob")})
    await third.async_restore_credential_names()
    assert third.credential_names.passcodes == {}
    assert third.credential_names.cards[4].name == "Fob"


async def test_credential_names_are_not_fetched_when_nothing_uses_them(
    hass, api, monkeypatch
):
    """Three cloud calls per lock per slow interval only buy something when the
    Credentials sensor is on or a local transport manages credentials."""
    coordinator = _coordinator(hass, api)
    lister = AsyncMock(return_value=[])
    monkeypatch.setattr(api, "list_passcodes", lister)
    monkeypatch.setattr(api, "list_cards", lister)
    monkeypatch.setattr(api, "list_fingerprints", lister)

    await coordinator._maybe_refresh_credentials(dt_util.now())

    lister.assert_not_awaited()
