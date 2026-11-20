"""Transport ordering and availability in coordinator.lock / unlock.

Each transport's availability and its control coroutine are stubbed, so these
tests assert only the selection logic: which available transport is tried, in
what order, that the first success wins, and that nothing available fails
honestly.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock import coordinator as coord_mod
from custom_components.ttlock.api import RequestFailed, TTLockApi
from custom_components.ttlock.ble import BleData
from custom_components.ttlock.ble_protocol import (
    Command,
    Frame,
    LockStatus,
)
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_CLOUD_ENABLED,
    CONF_TRANSPORT_ORDER,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
    TRANSPORT_WIFI_LOCAL,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import LockDetail, LockSummary, OnOff
from custom_components.ttlock.store import LockStateStore
from custom_components.ttlock.wifi_proxy import (
    OP_LOCK,
    WifiCredentials,
    WifiProxy,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .const import MOCK_LOCK_AES_KEY, MOCK_LOCK_MAC, MOCK_LOCK_VERSION
from .test_wifi_proxy import CREDS, MAC, FakeLock, _connect, _end, _wait


def _set_available(monkeypatch, *, ble: bool, wifi: bool, cloud: bool) -> None:
    """Force each transport's availability predicate on the coordinator class."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: ble)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: ble)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: wifi)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: cloud)
    )


def _stub_actions(
    monkeypatch, coordinator, *, ble=True, wifi=True, cloud=True
) -> dict[str, AsyncMock]:
    """Replace each transport's control coroutine with an AsyncMock."""
    mocks = {
        TRANSPORT_BLE: AsyncMock(return_value=ble),
        TRANSPORT_WIFI_LOCAL: AsyncMock(return_value=wifi),
        TRANSPORT_CLOUD: AsyncMock(return_value=cloud),
    }
    monkeypatch.setattr(coordinator, "async_control_ble", mocks[TRANSPORT_BLE])
    monkeypatch.setattr(
        coordinator, "_async_control_wifi_local", mocks[TRANSPORT_WIFI_LOCAL]
    )
    monkeypatch.setattr(coordinator, "_async_control_cloud", mocks[TRANSPORT_CLOUD])
    return mocks


async def test_default_order_prefers_ble_when_available(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True, wifi=True, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once_with(unlock=True)
    mocks[TRANSPORT_WIFI_LOCAL].assert_not_awaited()
    mocks[TRANSPORT_CLOUD].assert_not_awaited()
    assert coordinator.data.locked is False


async def test_skips_unavailable_and_uses_wifi_local(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=False, wifi=True, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.lock()

    mocks[TRANSPORT_BLE].assert_not_awaited()
    mocks[TRANSPORT_WIFI_LOCAL].assert_awaited_once_with(unlock=False)
    mocks[TRANSPORT_CLOUD].assert_not_awaited()
    assert coordinator.data.locked is True


async def test_falls_through_when_a_transport_fails(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True, wifi=True, cloud=True)
    # BLE is available but declines; wifi_local then succeeds.
    mocks = _stub_actions(monkeypatch, coordinator, ble=False, wifi=True)

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once()
    mocks[TRANSPORT_WIFI_LOCAL].assert_awaited_once()
    mocks[TRANSPORT_CLOUD].assert_not_awaited()
    assert coordinator.data.locked is False


async def test_honours_a_custom_order(coordinator, monkeypatch):
    coordinator._transport_order = [
        TRANSPORT_CLOUD,
        TRANSPORT_WIFI_LOCAL,
        TRANSPORT_BLE,
    ]
    _set_available(monkeypatch, ble=True, wifi=True, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.lock()

    mocks[TRANSPORT_CLOUD].assert_awaited_once()
    mocks[TRANSPORT_BLE].assert_not_awaited()
    mocks[TRANSPORT_WIFI_LOCAL].assert_not_awaited()


async def test_a_saved_transport_this_version_lacks_is_skipped(hass, api, monkeypatch):
    """An order saved by another version may name a transport this one lacks."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_TRANSPORT_ORDER: ["carrier_pigeon", TRANSPORT_CLOUD]},
    )
    entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=7252408,
        lockAlias="Test Lock",
        lockMac=MOCK_LOCK_MAC,
        hasGateway=1,
        userType="110301",
    )
    coord = LockUpdateCoordinator(
        hass, entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
    )
    _set_available(monkeypatch, ble=False, wifi=False, cloud=True)
    mocks = _stub_actions(monkeypatch, coord, cloud=False)

    assert coord.available_transports == [TRANSPORT_CLOUD]
    assert coord.preferred_control_transport == TRANSPORT_CLOUD
    with pytest.raises(HomeAssistantError) as excinfo:
        await coord.unlock()

    assert excinfo.value.translation_placeholders == {
        "name": coord.data.name,
        "transports": TRANSPORT_CLOUD,
    }
    mocks[TRANSPORT_CLOUD].assert_awaited_once_with(unlock=True)


async def test_no_control_transport_when_none_in_the_order_can_act(
    coordinator, monkeypatch
):
    coordinator._transport_order = [TRANSPORT_BLE]
    _set_available(monkeypatch, ble=False, wifi=False, cloud=False)

    assert coordinator.preferred_control_transport is None
    assert coordinator.control_cloud_free is False


@pytest.mark.parametrize("first", [TRANSPORT_WIFI_LOCAL, TRANSPORT_BLE])
async def test_state_reads_follow_the_transport_order(coordinator, monkeypatch, first):
    """Both local reads can answer; only the first in the user's order is asked."""
    second = TRANSPORT_BLE if first == TRANSPORT_WIFI_LOCAL else TRANSPORT_WIFI_LOCAL
    coordinator._transport_order = [first, second, TRANSPORT_CLOUD]
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    monkeypatch.setattr(coordinator, "_ble_credentials", lambda: (None, b""))
    ble = AsyncMock(return_value=LockStatus(battery=80, locked=True, door_open=None))
    monkeypatch.setattr(coord_mod, "async_read_status", ble)
    proxy = MagicMock()
    proxy.is_takeover_ready.return_value = True
    proxy.async_status = AsyncMock(return_value=(False, 70))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    cloud = AsyncMock(side_effect=AssertionError("cloud read"))
    monkeypatch.setattr(coordinator.api, "get_lock_state", cloud)

    status = await coordinator.async_read_lock_status(cloud=True)

    if first == TRANSPORT_WIFI_LOCAL:
        ble.assert_not_awaited()
        assert status == LockStatus(battery=70, locked=False, door_open=None)
    else:
        proxy.async_status.assert_not_awaited()
        assert status == LockStatus(battery=80, locked=True, door_open=None)


async def test_cloud_only_when_ble_and_wifi_unavailable(coordinator, monkeypatch):
    """BLE off, no proxy -> one cloud call."""
    _set_available(monkeypatch, ble=False, wifi=False, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.lock()

    mocks[TRANSPORT_CLOUD].assert_awaited_once_with(unlock=False)
    assert coordinator.data.locked is True


async def test_raises_when_nothing_is_available(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=False, wifi=False, cloud=False)
    _stub_actions(monkeypatch, coordinator)

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.unlock()

    assert excinfo.value.translation_key == "no_transport_to_unlock"


def test_admin_ps_decodes_from_the_cached_admin_password(coordinator):
    assert coordinator.admin_ps is None  # nothing cached yet
    coordinator._ble_admin_pwd = "12345678"  # short -> already plain
    assert coordinator.admin_ps == 12345678


def test_wifi_credentials_none_until_every_field_is_present(coordinator, monkeypatch):
    monkeypatch.setattr(
        type(coordinator.api), "uid", property(lambda self: 777), raising=False
    )
    assert coordinator.wifi_credentials() is None

    coordinator._ble_version = MOCK_LOCK_VERSION
    coordinator._ble_key = MOCK_LOCK_AES_KEY
    coordinator._ble_unlock_key = "999"  # short -> plain
    assert coordinator.wifi_credentials() is None  # still no admin password

    coordinator._ble_admin_pwd = "4242"
    creds = coordinator.wifi_credentials()
    assert creds is not None
    assert creds.admin_ps == 4242
    assert creds.unlock_key == "999"
    assert creds.uid == 777
    assert len(creds.aes_key) == 16


def test_credentials_that_do_not_decode_are_not_used(coordinator, monkeypatch):
    """A garbled key or admin password gives no credentials, never wrong ones."""
    monkeypatch.setattr(
        type(coordinator.api), "uid", property(lambda self: 777), raising=False
    )
    coordinator._ble_version = MOCK_LOCK_VERSION
    coordinator._ble_key = MOCK_LOCK_AES_KEY
    coordinator._ble_unlock_key = "999"
    coordinator._ble_admin_pwd = "4242"
    assert coordinator.wifi_credentials() is not None

    garbled = base64.b64encode(b"12,not-a-byte").decode("ascii")
    coordinator._ble_admin_pwd = garbled
    assert coordinator.admin_ps is None
    assert coordinator.wifi_credentials() is None

    coordinator._ble_admin_pwd = "4242"
    coordinator._ble_unlock_key = garbled
    assert coordinator._ble_control_credentials() is None
    assert coordinator.wifi_credentials() is None


async def test_wifi_local_reconnect_timeout_falls_through_to_cloud(
    hass, coordinator, monkeypatch
):
    """A recent wifi_local client whose session never returns falls through.

    With a real WifiProxy that knows the lock as a recent reconnecting client but
    has no live session, wifi_local is *attempted* (so it isn't skipped to BLE's
    timeout), waits out RECONNECT_WAIT, declines, and the drive moves on to cloud.
    """
    monkeypatch.setattr("custom_components.ttlock.wifi_proxy.RECONNECT_WAIT", 0.05)

    proxy = WifiProxy()
    creds = WifiCredentials(
        aes_key=bytes(16), admin_ps=4242, unlock_key="999", uid=777, lock_flag_pos=0
    )
    proxy.register_lock(
        coordinator.lock_id,
        b"\x00\x00\x00\x00\x00\x00",
        lambda: False,  # cloud off -> a takeover lock
        lambda: creds,
    )
    # Recently seen but no live session: a reconnecting client mid-blip.
    proxy._by_id[coordinator.lock_id].last_seen = dt_util.utcnow()
    hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    # BLE off; cloud on. wifi_local availability is the real drivable predicate.
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    coordinator._cloud_enabled_option = True
    assert coordinator.wifi_local_drivable is True  # attempted, not skipped

    # Spy on the real wifi_local control so we can prove it was actually tried
    # (a predicate that skipped it would still reach the cloud, so asserting the
    # cloud call alone is not enough).
    wifi_spy = AsyncMock(wraps=coordinator._async_control_wifi_local)
    monkeypatch.setattr(coordinator, "_async_control_wifi_local", wifi_spy)
    cloud_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator, "_async_control_cloud", cloud_mock)

    await coordinator.unlock()

    # wifi_local was tried (it is drivable) but its session never returned, so the
    # drive fell through to the cloud.
    wifi_spy.assert_awaited_once_with(unlock=True)
    cloud_mock.assert_awaited_once_with(unlock=True)
    assert coordinator.data.locked is False


async def test_every_tried_transport_failing_raises(coordinator, monkeypatch):
    """An automation must never take a failed unlock for a done one."""
    _set_available(monkeypatch, ble=True, wifi=False, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator, ble=False, cloud=False)

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.unlock()

    assert excinfo.value.translation_key == "unlock_failed"
    assert excinfo.value.translation_placeholders == {
        "name": coordinator.data.name,
        "transports": "ble, cloud",
    }
    mocks[TRANSPORT_CLOUD].assert_awaited_once()
    assert coordinator.data.locked is None


async def test_a_transport_that_raises_falls_through(coordinator, monkeypatch):
    """A cloud error (or any other) on one transport never aborts the chain."""
    coordinator._transport_order = [
        TRANSPORT_CLOUD,
        TRANSPORT_BLE,
        TRANSPORT_WIFI_LOCAL,
    ]
    _set_available(monkeypatch, ble=True, wifi=False, cloud=True)
    mocks = _stub_actions(monkeypatch, coordinator)
    mocks[TRANSPORT_CLOUD].side_effect = RequestFailed("gateway offline")

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once_with(unlock=True)
    assert coordinator.last_transport == TRANSPORT_BLE
    assert coordinator.data.locked is False


async def test_a_local_unlock_starts_the_auto_lock_check(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True, wifi=False, cloud=True)
    _stub_actions(monkeypatch, coordinator)
    coordinator.data.auto_lock_seconds = 30

    await coordinator.unlock()

    assert coordinator._auto_lock_task is not None
    coordinator._cancel_auto_lock()


async def test_a_cloud_unlock_leaves_the_auto_lock_check_to_the_webhook(
    coordinator, monkeypatch
):
    _set_available(monkeypatch, ble=False, wifi=False, cloud=True)
    _stub_actions(monkeypatch, coordinator)
    coordinator.data.auto_lock_seconds = 30

    await coordinator.unlock()

    assert coordinator._auto_lock_task is None


async def test_the_auto_lock_check_never_asks_the_cloud_when_off(
    coordinator, monkeypatch
):
    coordinator._cloud_enabled_option = False
    monkeypatch.setattr(
        LockUpdateCoordinator, "connectable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator, "async_read_state_ble", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        coordinator, "async_read_state_wifi_local", AsyncMock(return_value=(True, 80))
    )
    cloud = AsyncMock(side_effect=AssertionError("cloud asked"))
    monkeypatch.setattr(coordinator.api, "get_lock_state", cloud)
    coordinator.data.locked = False

    await coordinator._async_verify_auto_lock()

    assert coordinator.data.locked is True
    assert coordinator.data.last_reason == "Auto Lock"
    cloud.assert_not_awaited()


async def test_a_takeover_session_reads_a_stale_lock(hass, coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "connectable", property(lambda self: True)
    )
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)

    coordinator.async_wifi_local_changed()  # never read: stale
    await hass.async_block_till_done()
    refresh.assert_awaited_once()

    coordinator._async_note_verified()
    coordinator.async_wifi_local_changed()  # just read: fresh
    await hass.async_block_till_done()
    refresh.assert_awaited_once()

    coordinator.async_wifi_local_event()  # the bolt may have moved
    await hass.async_block_till_done()
    assert refresh.await_count == 2


async def test_bluetooth_control_sends_nothing_when_off_or_uncredentialled(
    hass, api, monkeypatch
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    control = AsyncMock()
    monkeypatch.setattr(coord_mod, "async_control", control)
    _set_available(monkeypatch, ble=True, wifi=False, cloud=False)

    coord._ble_unlock_key = None  # lock/detail has not supplied it yet
    assert await coord.async_control_ble(unlock=True) is False

    coord._ble_unlock_key = base64.b64encode(b"12,not-a-byte").decode("ascii")
    assert await coord.async_control_ble(unlock=True) is False

    coord._ble_unlock_key = "2345"  # credentials fine, but control turned off
    _set_available(monkeypatch, ble=False, wifi=False, cloud=False)
    assert await coord.async_control_ble(unlock=True) is False
    control.assert_not_awaited()

    _set_available(monkeypatch, ble=True, wifi=False, cloud=False)
    assert await coord.async_control_ble(unlock=True) is True
    control.assert_awaited_once()


async def test_lock_over_wifi_local_throws_the_bolt(hass, coordinator, monkeypatch):
    proxy = WifiProxy()
    proxy.register_lock(coordinator.lock_id, MAC, lambda: False, lambda: CREDS)
    hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    fake = FakeLock()
    fake.locked = False
    task = _connect(proxy, fake)
    await _wait(lambda: proxy.is_takeover_ready(coordinator.lock_id))
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    coordinator.data.locked = False

    await coordinator.lock()

    assert OP_LOCK in fake.commands
    assert fake.locked is True
    assert coordinator.data.locked is True
    assert coordinator.last_transport == TRANSPORT_WIFI_LOCAL
    await _end(fake, task)


async def test_a_wifi_local_attempt_without_the_proxy_fails_quietly(coordinator):
    """The proxy goes with the last WiFi entry; an attempt then reports failure."""
    assert coordinator.hass.data.get(DOMAIN, {}).get(TT_WIFI_PROXY) is None
    assert await coordinator._async_control_wifi_local(unlock=True) is False


async def test_a_failing_listener_never_breaks_a_proxy_session(
    coordinator, monkeypatch
):
    listeners = Mock(side_effect=RuntimeError("entity failed"))
    monkeypatch.setattr(coordinator, "async_update_listeners", listeners)

    coordinator.async_wifi_local_changed()  # must not raise into the proxy

    listeners.assert_called_once()


# --- end to end through the real Bluetooth session ----------------------------


def _ble_coordinator(hass, api, monkeypatch, *, cloud: bool = True):
    """A lock in range with every credential, each value in a distinct field."""
    monkeypatch.setattr(TTLockApi, "uid", 7, raising=False)
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_BLUETOOTH_ENABLED: True,
            CONF_BLE_CONTROL: True,
            CONF_CLOUD_ENABLED: cloud,
        },
    )
    entry.add_to_hass(hass)
    summary = LockSummary(
        lockId=7252408,
        lockAlias="Test Lock",
        lockMac=MOCK_LOCK_MAC,
        hasGateway=1,
        userType="110301",
    )
    coord = LockUpdateCoordinator(
        hass, entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
    )
    coord._ble_version = MOCK_LOCK_VERSION
    coord._ble_key = MOCK_LOCK_AES_KEY
    coord._ble_unlock_key = "2345"
    coord._ble_admin_pwd = "4242"
    coord._ble_lock_flag_pos = 0x0A
    coord.ble = BleData(rssi=-55, connectable=True)
    return coord


def _sent(speaking_lock, aes_key) -> dict[int, bytes]:
    frames = (Frame.decode(raw, aes_key=aes_key) for raw in speaking_lock.gatt.received)
    return {frame.command: frame.data for frame in frames}


async def test_ble_unlock_puts_uid_and_flag_pos_in_their_fields(
    hass, api, mock_bluetooth, monkeypatch, speaking_lock, aes_key
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    speaking_lock.answer(
        Command.RESPONSE,
        bytes([Command.CHECK_USER_TIME, 0x01, 0, 0, 0x03, 0xE8]),
        key=aes_key,
        to=Command.CHECK_USER_TIME,
    )
    speaking_lock.answer(
        Command.RESPONSE, bytes([Command.UNLOCK, 0x01]), key=aes_key, to=Command.UNLOCK
    )

    await coord.unlock()
    coord._cancel_auto_lock()

    sent = _sent(speaking_lock, aes_key)
    assert sent[Command.CHECK_USER_TIME][10:13] == bytes([0, 0, 0x0A])
    assert sent[Command.CHECK_USER_TIME][13:17] == (7).to_bytes(4, "big")
    assert coord.last_transport == TRANSPORT_BLE
    assert coord.ble_proven


async def test_a_refused_ble_handshake_falls_back_to_the_cloud(
    hass, api, mock_bluetooth, monkeypatch, speaking_lock, aes_key
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    speaking_lock.answer(
        Command.RESPONSE,
        bytes([Command.CHECK_USER_TIME, 0x00]),
        key=aes_key,
        to=Command.CHECK_USER_TIME,
    )
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coord.api, "unlock", cloud)

    await coord.unlock()

    cloud.assert_awaited_once()
    assert coord.last_transport == TRANSPORT_CLOUD


async def test_an_unreachable_lock_is_not_dialled(
    hass, api, mock_bluetooth, monkeypatch
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    coord.ble = BleData()
    ble = AsyncMock(return_value=True)
    monkeypatch.setattr(coord, "async_control_ble", ble)
    monkeypatch.setattr(coord.api, "unlock", AsyncMock(return_value=True))

    await coord.unlock()

    ble.assert_not_awaited()


async def test_cloud_off_and_a_failed_bluetooth_unlock_raises(
    hass, api, mock_bluetooth, monkeypatch
):
    coord = _ble_coordinator(hass, api, monkeypatch, cloud=False)
    monkeypatch.setattr(coord, "async_control_ble", AsyncMock(return_value=False))

    with pytest.raises(HomeAssistantError) as excinfo:
        await coord.unlock()

    assert excinfo.value.translation_key == "unlock_failed"
    assert coord.data.locked is not False


def test_an_unknown_on_off_in_the_detail_is_not_read_as_off(coordinator):
    coordinator._apply_detail(coordinator.data, LockDetail(lockSound=OnOff.unknown))
    assert coordinator.data.lock_sound is None
