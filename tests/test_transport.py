"""Transport ordering and availability in coordinator.lock / unlock.

Each transport's availability and its control coroutine are stubbed, so these
tests assert only the selection logic: which available transport is tried, in
what order, that the first success wins, and that a failure fails honestly.
"""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock import coordinator as coord_mod
from custom_components.ttlock.api import RequestFailed, TTLockApi
from custom_components.ttlock.ble import BleData
from custom_components.ttlock.ble_protocol import Command, Frame
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_TRANSPORT_ORDER,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_CLOUD,
)
from custom_components.ttlock.coordinator import LockUpdateCoordinator
from custom_components.ttlock.models import LockDetail, LockSummary, OnOff
from custom_components.ttlock.store import LockStateStore
from homeassistant.exceptions import HomeAssistantError

from .const import MOCK_LOCK_AES_KEY, MOCK_LOCK_MAC, MOCK_LOCK_VERSION


def _set_available(monkeypatch, *, ble: bool) -> None:
    """Force Bluetooth's availability predicates on the coordinator class."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: ble)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: ble)
    )


def _stub_actions(
    monkeypatch, coordinator, *, ble=True, cloud=True
) -> dict[str, AsyncMock]:
    """Replace each transport's control coroutine with an AsyncMock."""
    mocks = {
        TRANSPORT_BLE: AsyncMock(return_value=ble),
        TRANSPORT_CLOUD: AsyncMock(return_value=cloud),
    }
    monkeypatch.setattr(coordinator, "async_control_ble", mocks[TRANSPORT_BLE])
    monkeypatch.setattr(coordinator, "_async_control_cloud", mocks[TRANSPORT_CLOUD])
    return mocks


async def test_default_order_prefers_ble_when_available(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once_with(unlock=True)
    mocks[TRANSPORT_CLOUD].assert_not_awaited()
    assert coordinator.data.locked is False


async def test_falls_through_when_a_transport_fails(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True)
    # BLE is available but declines; the cloud then succeeds.
    mocks = _stub_actions(monkeypatch, coordinator, ble=False)

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once()
    mocks[TRANSPORT_CLOUD].assert_awaited_once()
    assert coordinator.data.locked is False


async def test_honours_a_custom_order(coordinator, monkeypatch):
    coordinator._transport_order = [TRANSPORT_CLOUD, TRANSPORT_BLE]
    _set_available(monkeypatch, ble=True)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.lock()

    mocks[TRANSPORT_CLOUD].assert_awaited_once()
    mocks[TRANSPORT_BLE].assert_not_awaited()


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
    _set_available(monkeypatch, ble=False)
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
    _set_available(monkeypatch, ble=False)

    assert coordinator.preferred_control_transport is None
    assert coordinator.control_cloud_free is False


async def test_cloud_only_when_ble_unavailable(coordinator, monkeypatch):
    """BLE off -> one cloud call."""
    _set_available(monkeypatch, ble=False)
    mocks = _stub_actions(monkeypatch, coordinator)

    await coordinator.lock()

    mocks[TRANSPORT_BLE].assert_not_awaited()
    mocks[TRANSPORT_CLOUD].assert_awaited_once_with(unlock=False)
    assert coordinator.data.locked is True


async def test_every_tried_transport_failing_raises(coordinator, monkeypatch):
    """An automation must never take a failed unlock for a done one."""
    _set_available(monkeypatch, ble=True)
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
    coordinator._transport_order = [TRANSPORT_CLOUD, TRANSPORT_BLE]
    _set_available(monkeypatch, ble=True)
    mocks = _stub_actions(monkeypatch, coordinator)
    mocks[TRANSPORT_CLOUD].side_effect = RequestFailed("gateway offline")

    await coordinator.unlock()

    mocks[TRANSPORT_BLE].assert_awaited_once_with(unlock=True)
    assert coordinator.last_transport == TRANSPORT_BLE
    assert coordinator.data.locked is False


async def test_a_local_unlock_starts_the_auto_lock_check(coordinator, monkeypatch):
    _set_available(monkeypatch, ble=True)
    _stub_actions(monkeypatch, coordinator)
    coordinator.data.auto_lock_seconds = 30

    await coordinator.unlock()

    assert coordinator._auto_lock_task is not None
    coordinator._cancel_auto_lock()


async def test_a_cloud_unlock_leaves_the_auto_lock_check_to_the_webhook(
    coordinator, monkeypatch
):
    _set_available(monkeypatch, ble=False)
    _stub_actions(monkeypatch, coordinator)
    coordinator.data.auto_lock_seconds = 30

    await coordinator.unlock()

    assert coordinator._auto_lock_task is None


async def test_bluetooth_control_sends_nothing_when_off_or_uncredentialled(
    hass, api, monkeypatch
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    control = AsyncMock()
    monkeypatch.setattr(coord_mod, "async_control", control)
    _set_available(monkeypatch, ble=True)

    coord._ble_unlock_key = None  # lock/detail has not supplied it yet
    assert await coord.async_control_ble(unlock=True) is False

    coord._ble_unlock_key = base64.b64encode(b"12,not-a-byte").decode("ascii")
    assert await coord.async_control_ble(unlock=True) is False

    coord._ble_unlock_key = "2345"  # credentials fine, but control turned off
    _set_available(monkeypatch, ble=False)
    assert await coord.async_control_ble(unlock=True) is False
    control.assert_not_awaited()

    _set_available(monkeypatch, ble=True)
    assert await coord.async_control_ble(unlock=True) is True
    control.assert_awaited_once()


# --- end to end through the real Bluetooth session ----------------------------


def _ble_coordinator(hass, api, monkeypatch):
    """A lock in range with every credential, each value in a distinct field."""
    monkeypatch.setattr(TTLockApi, "uid", 7, raising=False)
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={CONF_BLUETOOTH_ENABLED: True, CONF_BLE_CONTROL: True},
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


def test_an_unknown_on_off_in_the_detail_is_not_read_as_off(coordinator):
    coordinator._apply_detail(coordinator.data, LockDetail(lockSound=OnOff.unknown))
    assert coordinator.data.lock_sound is None
