"""Transport ordering and availability in coordinator.lock / unlock.

Each transport's availability and its control coroutine are stubbed, so these
tests assert only the selection logic: which available transport is tried, in
what order, that the first success wins, and that nothing available fails
honestly.
"""

from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import UTC, datetime
import logging
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from pytest_homeassistant.common import MockConfigEntry

from custom_components.ttlock import coordinator as coord_mod, lock_commands
from custom_components.ttlock.api import RequestFailed, TTLockApi
from custom_components.ttlock.ble import BleData, BleError, BleNoResponse
from custom_components.ttlock.ble_protocol import (
    PASSCODE_TYPE_COUNT,
    PASSCODE_TYPE_PERIOD,
    AutoLockConfig,
    Command,
    CredentialError,
    Frame,
    LockStatus,
    card_add_by_number_data,
    card_cyclic_add_data,
    card_cyclic_clear_data,
    card_modify_data,
    fr_cyclic_add_data,
    fr_cyclic_clear_data,
    fr_modify_data,
    lock_timezone,
    passcode_add_cyclic_data,
    passcode_add_timed_data,
    passcode_delete_data,
    passcode_modify_data,
    time_calibrate_data,
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
from custom_components.ttlock.coordinator import (
    LocalOutcomeUnknown,
    LockUpdateCoordinator,
    NoLocalAdminTransport,
)
from custom_components.ttlock.lock_commands import CommandRejected
from custom_components.ttlock.models import Features, LockDetail, LockSummary, OnOff
from custom_components.ttlock.number import AutoLockDelay
from custom_components.ttlock.store import LockStateStore
from custom_components.ttlock.switch import AutoLock, LockSound, TamperAlert
from custom_components.ttlock.wifi_proxy import (
    OP_CHECK_RANDOM,
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


async def test_the_local_settings_read_follows_the_transport_order(
    coordinator, monkeypatch
):
    """The read-only admin op behind the settings read honours the order too."""
    coordinator._transport_order = [
        TRANSPORT_WIFI_LOCAL,
        TRANSPORT_BLE,
        TRANSPORT_CLOUD,
    ]
    _enable_ble_admin(monkeypatch, coordinator, error=AssertionError("BLE first"))
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, "read"))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    tried = MagicMock(wraps=coord_mod.async_admin_session)
    monkeypatch.setattr(coord_mod, "async_admin_session", tried)

    result = await coordinator.async_run_local_admin_op(
        "read settings", AsyncMock(), idempotent=True, read_only=True
    )

    assert result == "read"
    tried.assert_not_called()


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


# --- admin feature-op fan-out (set_auto_lock_seconds, sync_clock) ------------


class _FakeAdminSession:
    """Scripted AdminSession: returns canned reply payloads, records calls."""

    def __init__(self, replies: dict[int, bytes | list[bytes]]) -> None:
        self._replies = replies
        self.calls: list[tuple[int, bytes]] = []

    async def command(self, opcode: int, data: bytes = b"") -> bytes:
        self.calls.append((int(opcode), data))
        reply = self._replies[int(opcode)]
        return reply.pop(0) if isinstance(reply, list) else reply


def _env(opcode: int, *, ok: bool = True, data: bytes = b"") -> bytes:
    return bytes([opcode, 0x01 if ok else 0x00]) + data


def _fake_ble_credentials(monkeypatch, coordinator) -> None:
    """Credentials the faked admin session ignores, present so BLE is tried."""
    monkeypatch.setattr(
        coordinator, "_ble_control_credentials", lambda: (None, b"", "0", 0, 0)
    )
    monkeypatch.setattr(LockUpdateCoordinator, "admin_ps", property(lambda self: 0))


def _enable_ble_admin(monkeypatch, coordinator, *, session=None, error=None) -> None:
    """Make the BLE admin transport the one available local path."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    _fake_ble_credentials(monkeypatch, coordinator)

    @asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        if error is not None:
            raise error
        yield session

    monkeypatch.setattr(coord_mod, "async_admin_session", fake_session)


async def test_set_auto_lock_uses_ble_and_skips_cloud(coordinator, monkeypatch):
    session = _FakeAdminSession(
        {0x36: [_env(0x36), _env(0x36, data=bytes.fromhex("3a01002d00010384"))]}
    )
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)

    await coordinator.set_auto_lock_seconds(45)

    assert coordinator.data.auto_lock_seconds == 45
    assert session.calls[0] == (0x36, bytes.fromhex("02002d"))
    cloud.assert_not_awaited()


async def test_set_auto_lock_falls_back_to_cloud_when_ble_fails(
    coordinator, monkeypatch
):
    _enable_ble_admin(monkeypatch, coordinator, error=BleError("out of range"))
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
    )

    await coordinator.set_auto_lock_seconds(30)

    cloud.assert_awaited_once_with(coordinator.lock_id, 30)
    assert coordinator.data.auto_lock_seconds == 30


async def test_rejection_propagates_and_never_falls_back(coordinator, monkeypatch):
    session = _FakeAdminSession({0x36: [_env(0x36, ok=False)]})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
    )

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.set_auto_lock_seconds(45)

    assert excinfo.value.translation_key == "local_command_rejected"
    assert isinstance(excinfo.value.__cause__, CommandRejected)

    cloud.assert_not_awaited()  # a lock "no" is the answer, not a reason to retry


async def test_sync_clock_requires_a_local_transport(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )

    with pytest.raises(NoLocalAdminTransport):
        await coordinator.sync_clock(datetime(2026, 11, 16, 20, 55, 0))


async def test_add_passcode_records_the_name_locally(coordinator, monkeypatch):
    session = _FakeAdminSession({0x03: _env(0x03)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await coordinator.add_passcode_local("1234", name="Cleaner")

    names = [info.name for info in coordinator.credential_names.passcodes.values()]
    assert "Cleaner" in names
    # Stored under a synthetic negative id so it cannot collide with cloud ids.
    assert all(cid < 0 for cid in coordinator.credential_names.passcodes)


def _enable_wifi_admin(monkeypatch, coordinator, proxy) -> None:
    """Make WiFi-local the available local admin path, with a mocked proxy."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy


def _auto_lock_config():
    return AutoLockConfig(seconds=45)


async def test_admin_op_uses_wifi_when_ble_is_off(coordinator, monkeypatch):
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, _auto_lock_config()))
    _enable_wifi_admin(monkeypatch, coordinator, proxy)
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)

    await coordinator.set_auto_lock_seconds(45)

    proxy.async_admin_op.assert_awaited_once()
    cloud.assert_not_awaited()
    assert coordinator.data.auto_lock_seconds == 45


async def test_wifi_not_attempted_falls_through_to_cloud(coordinator, monkeypatch):
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(False, None))  # no ready session
    _enable_wifi_admin(monkeypatch, coordinator, proxy)
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
    )
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)

    await coordinator.set_auto_lock_seconds(20)

    proxy.async_admin_op.assert_awaited_once()
    cloud.assert_awaited_once_with(coordinator.lock_id, 20)


async def test_ble_failure_moves_on_to_wifi(coordinator, monkeypatch):
    # BLE enabled but its session errors; WiFi is the next local transport.
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    _fake_ble_credentials(monkeypatch, coordinator)
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )

    @asynccontextmanager
    async def failing_session(*_a, **_k):
        raise BleError("out of range")
        yield  # pragma: no cover

    monkeypatch.setattr(coord_mod, "async_admin_session", failing_session)
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, _auto_lock_config()))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    await coordinator.set_auto_lock_seconds(45)

    proxy.async_admin_op.assert_awaited_once()
    assert coordinator.data.auto_lock_seconds == 45


async def test_clear_passcodes_drops_locally_recorded_names(coordinator, monkeypatch):
    add = _FakeAdminSession({0x03: _env(0x03)})
    _enable_ble_admin(monkeypatch, coordinator, session=add)
    await coordinator.add_passcode_local("1234", name="Cleaner")
    assert any(cid < 0 for cid in coordinator.credential_names.passcodes)

    clear = _FakeAdminSession({0x03: _env(0x03)})
    _enable_ble_admin(monkeypatch, coordinator, session=clear)
    await coordinator.clear_passcodes_local()

    assert not any(cid < 0 for cid in coordinator.credential_names.passcodes)


async def test_number_can_still_disable_below_the_locks_minimum(
    coordinator, monkeypatch
):
    # The lock's own minimum is 1; disabling with 0 must still reach the lock,
    # i.e. the entity min stays 0.
    number = AutoLockDelay(coordinator)
    number._update_from_coordinator()
    assert number.native_min_value == 0  # 0 (disable) stays reachable
    assert number.native_max_value == 900

    disable = _FakeAdminSession(
        {0x36: [_env(0x36), _env(0x36, data=bytes.fromhex("3a010000000103e8"))]}
    )
    _enable_ble_admin(monkeypatch, coordinator, session=disable)
    await number.async_set_native_value(0)
    assert coordinator.data.auto_lock_seconds == 0


# --- IC card admin-op fan-out ------------------------------------------------


async def test_add_card_uses_ble_and_records_name(coordinator, monkeypatch):
    session = _FakeAdminSession({0x05: _env(0x05)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await coordinator.add_card_local("1234567890", name="Front fob")

    # The 0x05 ADD rode the BLE admin session.
    assert session.calls[0][0] == 0x05
    assert session.calls[0][1][0] == 0x02  # ADD sub-op
    # The name is recorded under a synthetic negative id; the number is not stored.
    names = [info.name for info in coordinator.credential_names.cards.values()]
    assert "Front fob" in names
    assert all(cid < 0 for cid in coordinator.credential_names.cards)
    assert "1234567890" not in repr(coordinator.credential_names.cards)


async def test_add_card_ble_failure_moves_on_to_wifi(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    _fake_ble_credentials(monkeypatch, coordinator)
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )

    @asynccontextmanager
    async def failing_session(*_a, **_k):
        raise BleError("out of range")
        yield  # pragma: no cover

    monkeypatch.setattr(coord_mod, "async_admin_session", failing_session)
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, None))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    await coordinator.delete_card_local("1234567890")

    proxy.async_admin_op.assert_awaited_once()


async def test_card_rejection_propagates_and_never_falls_back(coordinator, monkeypatch):
    # [05][00][02] -> LOCK_NO_PERMISSION: a clean "no", not a transport failure.
    session = _FakeAdminSession({0x05: _env(0x05, ok=False, data=b"\x02")})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, None))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.delete_card_local("1234567890")

    assert excinfo.value.translation_key == "local_command_rejected"
    cause = excinfo.value.__cause__
    assert isinstance(cause, CommandRejected)
    assert "not administrator - no permission" in str(cause)
    proxy.async_admin_op.assert_not_awaited()  # a lock "no" is the answer


async def test_clear_cards_drops_locally_recorded_names(coordinator, monkeypatch):
    add = _FakeAdminSession({0x05: _env(0x05)})
    _enable_ble_admin(monkeypatch, coordinator, session=add)
    await coordinator.add_card_local("1234567890", name="Fob")
    assert any(cid < 0 for cid in coordinator.credential_names.cards)

    clear = _FakeAdminSession({0x05: _env(0x05)})
    _enable_ble_admin(monkeypatch, coordinator, session=clear)
    await coordinator.clear_cards_local()

    assert not any(cid < 0 for cid in coordinator.credential_names.cards)


# --- Fingerprint admin-op fan-out --------------------------------------------


async def test_delete_fingerprint_uses_ble(coordinator, monkeypatch):
    session = _FakeAdminSession({0x06: _env(0x06)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await coordinator.delete_fingerprint_local("1234567890")

    assert session.calls[0][0] == 0x06
    assert session.calls[0][1][0] == 0x03  # DELETE sub-op
    # The number never reaches state or a stored repr.
    assert "1234567890" not in repr(coordinator.credential_names.fingerprints)


async def test_fingerprint_ble_failure_moves_on_to_wifi(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    _fake_ble_credentials(monkeypatch, coordinator)
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )

    @asynccontextmanager
    async def failing_session(*_a, **_k):
        raise BleError("out of range")
        yield  # pragma: no cover

    monkeypatch.setattr(coord_mod, "async_admin_session", failing_session)
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, None))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    await coordinator.delete_fingerprint_local("1234567890")

    proxy.async_admin_op.assert_awaited_once()


async def test_fingerprint_rejection_propagates_and_never_falls_back(
    coordinator, monkeypatch
):
    # [06][00][1a] -> 26 = FR_NOT_EXIST: a clean "no", not a transport failure.
    session = _FakeAdminSession({0x06: _env(0x06, ok=False, data=b"\x1a")})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, None))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.delete_fingerprint_local("1234567890")

    assert excinfo.value.translation_key == "local_command_rejected"
    cause = excinfo.value.__cause__
    assert isinstance(cause, CommandRejected)
    assert "fingerprint does not exist" in str(cause)
    proxy.async_admin_op.assert_not_awaited()


async def test_clear_fingerprints_drops_locally_recorded_names(
    coordinator, monkeypatch
):
    clear = _FakeAdminSession({0x06: _env(0x06)})
    _enable_ble_admin(monkeypatch, coordinator, session=clear)
    await coordinator._record_local_name("fingerprints", "Thumb")
    assert any(cid < 0 for cid in coordinator.credential_names.fingerprints)

    await coordinator.clear_fingerprints_local()

    assert not any(cid < 0 for cid in coordinator.credential_names.fingerprints)


async def test_enroll_fingerprint_routes_to_wifi_and_records_name(
    coordinator, monkeypatch
):
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )
    proxy = AsyncMock()
    proxy.async_enroll_fingerprint = AsyncMock(return_value=(True, 1234567890))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    number = await coordinator.enroll_fingerprint_local(name="Thumb")

    assert number == 1234567890
    proxy.async_enroll_fingerprint.assert_awaited_once()
    # Name recorded under a synthetic negative id; the number is not stored.
    names = [info.name for info in coordinator.credential_names.fingerprints.values()]
    assert "Thumb" in names
    assert all(cid < 0 for cid in coordinator.credential_names.fingerprints)
    assert "1234567890" not in repr(coordinator.credential_names.fingerprints)


async def test_enroll_fingerprint_without_wifi_raises_before_sending(
    coordinator, monkeypatch
):
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: False)
    )
    proxy = AsyncMock()
    proxy.async_enroll_fingerprint = AsyncMock()
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    with pytest.raises(NoLocalAdminTransport):
        await coordinator.enroll_fingerprint_local()

    proxy.async_enroll_fingerprint.assert_not_awaited()  # nothing sent to the lock


# --- outcome-unknown, serialisation, translated errors ------------------------


# A 0x68 GET reply: battery, sub-op 1, switchItem and switchValue all clear.
_SWITCH_BITMASK = _env(0x68, data=bytes([0x3A, 0x01]) + bytes(8))


class _SilentSession:
    """An admin session whose lock takes each command and never answers."""

    def __init__(self) -> None:
        self.calls = 0

    async def command(self, opcode: int, data: bytes = b"") -> bytes:
        self.calls += 1
        raise BleNoResponse("no answer")


def _wifi_proxy_stub(coordinator, monkeypatch, *, result=None) -> AsyncMock:
    """A WiFi-local path that runs the op on a scripted session."""
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator,
        "wifi_credentials",
        lambda: WifiCredentials(
            aes_key=bytes(16), admin_ps=1, unlock_key="1", uid=1, lock_flag_pos=0
        ),
    )
    proxy = AsyncMock()
    proxy.async_admin_op = AsyncMock(return_value=(True, result))
    coordinator.hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    return proxy


async def test_a_lost_answer_to_an_add_is_never_rerun(coordinator, monkeypatch):
    """The card may be on the lock already: adding it again elsewhere would
    double-add it, and falling back to the cloud would too."""
    session = _SilentSession()
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
    )

    with pytest.raises(LocalOutcomeUnknown) as excinfo:
        await coordinator.add_card_local("1234567890", name="Fob")

    assert not isinstance(excinfo.value, NoLocalAdminTransport)
    assert excinfo.value.translation_key == "local_outcome_unknown"
    assert session.calls == 1
    proxy.async_admin_op.assert_not_awaited()
    assert not coordinator.credential_names.cards  # nothing recorded


async def test_a_lost_answer_over_wifi_is_never_rerun(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)

    async def sent_then_timed_out(lock_id, op, on_sent):
        on_sent()
        await op(_FakeAdminSession({0x68: [_SWITCH_BITMASK, _env(0x68)]}))
        raise TimeoutError

    proxy.async_admin_op.side_effect = sent_then_timed_out

    with pytest.raises(LocalOutcomeUnknown):
        await coordinator.set_tamper_alert(True)


async def test_a_lost_answer_to_a_read_moves_on(coordinator, monkeypatch):
    """A read can safely be asked again over the next transport."""
    _enable_ble_admin(monkeypatch, coordinator, session=_SilentSession())
    proxy = _wifi_proxy_stub(coordinator, monkeypatch, result=[])

    cards = await coordinator.async_run_local_admin_op(
        "list cards", lock_commands.list_cards, idempotent=True
    )
    assert cards == []
    proxy.async_admin_op.assert_awaited_once()


async def test_a_malformed_value_is_not_a_transport_failure(coordinator, monkeypatch):
    session = _FakeAdminSession({})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)

    with pytest.raises(ValueError):
        await coordinator.add_passcode_local("12")  # too short to frame

    assert session.calls == []
    proxy.async_admin_op.assert_not_awaited()


async def test_no_local_transport_is_a_translated_error(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )

    with pytest.raises(NoLocalAdminTransport) as excinfo:
        await coordinator.set_tamper_alert(True)

    assert isinstance(excinfo.value, HomeAssistantError)
    assert excinfo.value.translation_key == "no_local_transport"


async def test_concurrent_switch_writes_never_interleave(coordinator, monkeypatch):
    """0x68 is read-modify-write: interleaved, one write would undo the other."""
    active = 0
    overlapped = False

    class SlowSession(_FakeAdminSession):
        async def command(self, opcode: int, data: bytes = b"") -> bytes:
            nonlocal active, overlapped
            active += 1
            overlapped |= active > 1
            await asyncio.sleep(0.01)
            active -= 1
            return await super().command(opcode, data)

    bitmask = _SWITCH_BITMASK
    session = SlowSession({0x68: [bitmask, _env(0x68), bitmask, _env(0x68)]})
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await asyncio.gather(
        coordinator.set_tamper_alert(True), coordinator.set_passage_auto_unlock(True)
    )

    assert not overlapped
    assert [opcode for opcode, _ in session.calls] == [0x68] * 4


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


async def test_a_cancelled_waiting_unlock_stops_counting_as_waiting(
    coordinator, monkeypatch
):
    """A read gives way while a lock/unlock waits; a stale count would make
    every later read give way to an unlock that is not coming."""
    _set_available(monkeypatch, ble=True, wifi=False, cloud=False)
    _fake_ble_credentials(monkeypatch, coordinator)
    control = AsyncMock()
    monkeypatch.setattr(coord_mod, "async_control", control)

    async with coordinator._local_lock:
        task = asyncio.create_task(coordinator.async_control_ble(unlock=True))
        await _wait(lambda: coordinator._controls_waiting == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert coordinator._controls_waiting == 0
    control.assert_not_awaited()


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


async def test_ble_admin_op_puts_each_secret_in_its_own_field(
    hass, api, mock_bluetooth, monkeypatch, speaking_lock, aes_key
):
    coord = _ble_coordinator(hass, api, monkeypatch)
    speaking_lock.answer(
        Command.RESPONSE,
        bytes([Command.CHECK_ADMIN, 0x01, 0, 0, 0, 5]),
        key=aes_key,
        to=Command.CHECK_ADMIN,
    )
    speaking_lock.answer(
        Command.RESPONSE,
        bytes([Command.CHECK_RANDOM, 0x01]),
        key=aes_key,
        to=Command.CHECK_RANDOM,
    )
    speaking_lock.answer(
        Command.RESPONSE,
        bytes([Command.AUDIO_MANAGE, 0x01]),
        key=aes_key,
        to=Command.AUDIO_MANAGE,
    )
    assert not coord.ble_proven

    await coord.set_lock_sound(True)

    sent = _sent(speaking_lock, aes_key)
    # adminPs 4242 (4 BE), lockFlagPos 0x0A (3 BE), uid 7 (4 BE) - hand-written.
    assert sent[Command.CHECK_ADMIN] == bytes.fromhex("0000109200000a00000007")
    # token 5 + unlockKey 2345 = 2350
    assert sent[Command.CHECK_RANDOM] == (2350).to_bytes(4, "big")
    assert coord.ble_proven  # a completed admin op proves reachability
    assert coord.data.lock_sound is True


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


async def test_a_wifi_rejection_never_falls_back_to_the_cloud(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    proxy.async_admin_op.side_effect = CommandRejected("no")
    monkeypatch.setattr(
        LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
    )
    cloud = AsyncMock(return_value=True)
    monkeypatch.setattr(coordinator.api, "set_auto_lock", cloud)

    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.set_auto_lock_seconds(30)

    assert excinfo.value.translation_key == "local_command_rejected"
    cloud.assert_not_awaited()


async def test_a_local_auto_lock_set_trusts_the_value_the_lock_reads_back(
    coordinator, monkeypatch
):
    # The lock clamps 45 -> 30 (0x1e) and reports range 1..900.
    session = _FakeAdminSession(
        {0x36: [_env(0x36), _env(0x36, data=bytes.fromhex("3a01001e00010384"))]}
    )
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await coordinator.set_auto_lock_seconds(45)

    assert coordinator.data.auto_lock_seconds == 30


async def test_a_local_auto_lock_off_cancels_the_pending_check(
    coordinator, monkeypatch
):
    session = _FakeAdminSession(
        {0x36: [_env(0x36), _env(0x36, data=bytes.fromhex("3a01000000010384"))]}
    )
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    cancel = MagicMock()
    monkeypatch.setattr(coordinator, "_cancel_auto_lock", cancel)

    await coordinator.set_auto_lock_seconds(0)

    cancel.assert_called_once()


async def test_dates_are_written_in_the_locks_own_timezone(coordinator, monkeypatch):
    """The lock keeps no zone of its own: an aware date is rendered in its offset."""
    coordinator._timezone_raw_offset = 3_600_000  # UTC+1, from lock/detail
    session = _FakeAdminSession({0x05: _env(0x05), 0x43: _env(0x43)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    start = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)

    await coordinator.add_card_local("1234567890", start=start, end=end)
    await coordinator.sync_clock(start)

    lock_tz = lock_timezone(3_600_000)
    assert session.calls == [
        (0x05, card_add_by_number_data("1234567890", start=start, end=end, tz=lock_tz)),
        (0x43, time_calibrate_data(start, tz=lock_tz)),
    ]
    assert session.calls[1][1] == bytes.fromhex("1a0101090000")  # 09:00 local


# --- credential edits over a local transport ----------------------------------


def _count_admin_sessions(monkeypatch) -> list[int]:
    """Count the Bluetooth admin sessions opened, wrapping `_enable_ble_admin`'s."""
    opened: list[int] = []
    inner = coord_mod.async_admin_session

    @asynccontextmanager
    async def counting(*args, **kwargs):
        opened.append(1)
        async with inner(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(coord_mod, "async_admin_session", counting)
    return opened


async def test_local_passcode_edits_send_their_commands_in_the_locks_timezone(
    coordinator, monkeypatch
):
    coordinator._timezone_raw_offset = 3_600_000
    session = _FakeAdminSession({0x03: _env(0x03)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    start = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    lock_tz = lock_timezone(3_600_000)

    await coordinator.delete_passcode_local("1234", pwd_type=PASSCODE_TYPE_PERIOD)
    await coordinator.add_onetime_passcode_local("2345", start=start, end=end)
    await coordinator.add_cyclic_passcode_local("3456", start=start, cycle_type=0x30)
    await coordinator.modify_passcode_local(
        "4567", "5678", start=start, end=end, pwd_type=PASSCODE_TYPE_PERIOD
    )

    assert session.calls == [
        (0x03, passcode_delete_data("1234", PASSCODE_TYPE_PERIOD)),
        (
            0x03,
            passcode_add_timed_data(
                "2345", start, end, pwd_type=PASSCODE_TYPE_COUNT, tz=lock_tz
            ),
        ),
        (
            0x03,
            passcode_add_cyclic_data("3456", start=start, cycle_type=0x30, tz=lock_tz),
        ),
        (
            0x03,
            passcode_modify_data(
                "4567",
                "5678",
                start=start,
                end=end,
                pwd_type=PASSCODE_TYPE_PERIOD,
                tz=lock_tz,
            ),
        ),
    ]
    # Rendered at 09:00 in the lock's zone, not 08:00 UTC.
    assert session.calls[1][1] != passcode_add_timed_data(
        "2345", start, end, pwd_type=PASSCODE_TYPE_COUNT, tz=UTC
    )
    assert coordinator.credential_names.passcodes == {}  # no names were given


async def test_local_card_edits_send_their_commands(coordinator, monkeypatch):
    coordinator._timezone_raw_offset = 3_600_000
    session = _FakeAdminSession({0x05: _env(0x05), 0x70: _env(0x70)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    opened = _count_admin_sessions(monkeypatch)
    start = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    card = "1234567890"

    await coordinator.modify_card_validity_local(card, start, end)
    await coordinator.set_card_schedule_local(
        card, week_days=[1, 3], start_minute=480, end_minute=1020
    )
    await coordinator.clear_card_cyclic_local(card)

    assert session.calls == [
        (0x05, card_modify_data(card, start, end, tz=lock_timezone(3_600_000))),
        (0x70, card_cyclic_clear_data(card)),
        (0x70, card_cyclic_add_data(card, 1, 480, 1020)),
        (0x70, card_cyclic_add_data(card, 3, 480, 1020)),
        (0x70, card_cyclic_clear_data(card)),
    ]
    assert len(opened) == 3  # the whole schedule rewrite is one session


async def test_local_fingerprint_edits_send_their_commands(coordinator, monkeypatch):
    coordinator._timezone_raw_offset = 3_600_000
    session = _FakeAdminSession({0x06: _env(0x06), 0x70: _env(0x70)})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    opened = _count_admin_sessions(monkeypatch)
    start = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
    end = datetime(2026, 1, 2, 8, 0, tzinfo=UTC)
    print_number = "1234567890"

    await coordinator.modify_fingerprint_validity_local(print_number, start, end)
    await coordinator.set_fingerprint_schedule_local(
        print_number, week_days=[2, 6], start_minute=0, end_minute=1439
    )
    await coordinator.clear_fingerprint_cyclic_local(print_number)

    assert session.calls == [
        (
            0x06,
            fr_modify_data(print_number, start, end, tz=lock_timezone(3_600_000)),
        ),
        (0x70, fr_cyclic_clear_data(print_number)),
        (0x70, fr_cyclic_add_data(print_number, 2, 0, 1439)),
        (0x70, fr_cyclic_add_data(print_number, 6, 0, 1439)),
        (0x70, fr_cyclic_clear_data(print_number)),
    ]
    assert len(opened) == 3


async def test_a_malformed_card_number_is_never_sent_anywhere(coordinator, monkeypatch):
    session = _FakeAdminSession({})
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)

    with pytest.raises(ValueError, match="decimal digits"):
        await coordinator.clear_card_cyclic_local("12ab")

    assert session.calls == []
    proxy.async_admin_op.assert_not_awaited()


async def test_an_unusable_bluetooth_credential_moves_on_to_wifi(
    coordinator, monkeypatch, caplog
):
    _enable_ble_admin(
        monkeypatch, coordinator, error=CredentialError("admin password too long")
    )
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)

    with caplog.at_level(logging.WARNING):
        await coordinator.sync_clock(datetime(2026, 1, 1))

    proxy.async_admin_op.assert_awaited_once()
    assert "over ble failed (CredentialError)" in caplog.text


async def test_a_wifi_session_without_credentials_is_not_used(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    monkeypatch.setattr(coordinator, "wifi_credentials", lambda: None)

    with pytest.raises(NoLocalAdminTransport):
        await coordinator.set_tamper_alert(True)

    proxy.async_admin_op.assert_not_awaited()


async def test_a_cancelled_queued_write_stops_counting_as_waiting(
    coordinator, monkeypatch
):
    """A long read gives way while a write waits; a stale count would make
    every later read give way to a write that is not coming."""
    session = _FakeAdminSession({})
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    async with coordinator._admin_lock:
        task = asyncio.create_task(coordinator.sync_clock(datetime(2026, 1, 1)))
        await _wait(lambda: coordinator._ops_waiting["write"] == 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert coordinator._ops_waiting["write"] == 0
    assert session.calls == []


async def test_an_enrolment_with_no_session_after_all_records_nothing(
    coordinator, monkeypatch
):
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    proxy.async_enroll_fingerprint = AsyncMock(return_value=(False, None))

    with pytest.raises(NoLocalAdminTransport):
        await coordinator.enroll_fingerprint_local(name="Thumb")

    proxy.async_enroll_fingerprint.assert_awaited_once()
    assert not coordinator.credential_names.fingerprints


async def test_a_refused_wifi_handshake_is_not_a_sent_op(
    hass, coordinator, monkeypatch
):
    """The WiFi handshake runs inside the op's first command: a refusal there
    sent nothing, so a non-idempotent add still moves on instead of reporting
    an unknown outcome."""

    class RefusingLock(FakeLock):
        def _respond(self, raw: bytes) -> None:
            frame = self._decode(raw)
            if frame is not None and frame.command == OP_CHECK_RANDOM:
                self.commands.append(frame.command)
                self._reply(bytes([OP_CHECK_RANDOM, 0x00]))
                return
            super()._respond(raw)

    proxy = WifiProxy()
    proxy.register_lock(coordinator.lock_id, MAC, lambda: False, lambda: CREDS)
    hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy
    fake = RefusingLock()
    task = _connect(proxy, fake)
    await _wait(lambda: proxy.is_takeover_ready(coordinator.lock_id))
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: False)
    )
    monkeypatch.setattr(coordinator, "wifi_credentials", lambda: CREDS)

    with pytest.raises(NoLocalAdminTransport):
        await coordinator.add_card_local("1234567890", name="Fob")

    assert OP_CHECK_RANDOM in fake.commands
    assert 0x05 not in fake.commands  # the card add itself was never sent
    await _end(fake, task)


async def test_enrolment_errors_reach_the_user_translated(coordinator, monkeypatch):
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    proxy.async_enroll_fingerprint = AsyncMock(side_effect=TimeoutError())

    with pytest.raises(LocalOutcomeUnknown):
        await coordinator.enroll_fingerprint_local(name="Thumb")

    proxy.async_enroll_fingerprint.side_effect = CommandRejected("no")
    with pytest.raises(HomeAssistantError) as excinfo:
        await coordinator.enroll_fingerprint_local(name="Thumb")
    assert excinfo.value.translation_key == "local_command_rejected"
    assert not coordinator.credential_names.fingerprints


async def test_local_setting_writes_survive_a_restart(hass, coordinator, monkeypatch):
    """With the cloud gone, a restart must not bring back the old settings."""
    session = _FakeAdminSession(
        {
            0x62: _env(0x62),
            0x36: [_env(0x36), _env(0x36, data=bytes.fromhex("3a01001e00010384"))],
            0x68: [_SWITCH_BITMASK, _env(0x68)],
            0x66: [_env(0x66), _env(0x66)],
        }
    )
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    await coordinator.set_lock_sound(True)
    await coordinator.set_auto_lock_seconds(30)
    await coordinator.set_tamper_alert(True)
    await coordinator.set_passage_mode_local(
        week_days=[1], start_minute=0, end_minute=0, all_day=True
    )

    restarted = LockUpdateCoordinator(
        hass,
        coordinator.config_entry,
        coordinator.api,
        coordinator.summary,
        LockTrafficCapture(),
        coordinator._store,
    )
    await restarted.async_restore_lock_cache()
    assert restarted.data.lock_sound is True
    assert restarted.data.auto_lock_seconds == 30
    assert restarted.data.tamper_alert is True
    assert restarted.data.passage_mode_config == coordinator.data.passage_mode_config


def test_an_unreported_timezone_is_not_utc(coordinator):
    """A lock that never reported an offset is left on naive dates, not UTC."""
    assert coordinator.summary.timezoneRawOffset is None
    assert coordinator._lock_tz is None
    coordinator._timezone_raw_offset = 0
    assert coordinator._lock_tz is not None  # a reported 0 is UTC


# --- a lock/unlock never waits behind a long local read ------------------------


class _SlowPagedSession:
    """An AdminSession whose every page takes a moment, like a long log walk."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.calls = 0

    async def command(self, opcode: int, data: bytes = b"") -> bytes:
        self.calls += 1
        await asyncio.sleep(0.005)
        return _env(opcode)


async def _walk(session) -> str:
    for _ in range(100_000):
        await session.command(Command.GET_OPERATE_LOG, b"")
    return "done"


async def test_a_ble_unlock_interrupts_a_long_ble_read(coordinator, monkeypatch):
    """The read gives up the radio link at the next page; the unlock goes first."""
    events: list[str] = []
    session = _SlowPagedSession(events)
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    _fake_ble_credentials(monkeypatch, coordinator)

    @asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        try:
            yield session
        finally:
            events.append("read link closed")

    monkeypatch.setattr(coord_mod, "async_admin_session", fake_session)
    control = AsyncMock(side_effect=lambda *a, **k: events.append("unlocked"))
    monkeypatch.setattr(coord_mod, "async_control", control)

    read = asyncio.create_task(
        coordinator.async_run_local_admin_op(
            "read operate log", _walk, idempotent=True, read_only=True
        )
    )
    await _wait(lambda: session.calls >= 3)

    assert await asyncio.wait_for(coordinator.async_control_ble(unlock=True), 1.0)
    assert events == ["read link closed", "unlocked"]
    with pytest.raises(NoLocalAdminTransport):
        await asyncio.wait_for(read, 1.0)  # the read may be asked again


async def test_a_write_interrupts_a_long_ble_read(coordinator, monkeypatch):
    """The read closes its link at the next page, the write runs, the read resumes."""
    events: list[str] = []
    session = _SlowPagedSession(events)
    _enable_ble_admin(monkeypatch, coordinator, session=session)

    @asynccontextmanager
    async def fake_session(*_args, **_kwargs):
        try:
            yield session
        finally:
            events.append("link closed")

    monkeypatch.setattr(coord_mod, "async_admin_session", fake_session)

    async def write(_session) -> str:
        events.append("written")
        return "ok"

    read = asyncio.create_task(
        coordinator.async_run_local_admin_op(
            "read operate log", _walk, idempotent=True, read_only=True
        )
    )
    await _wait(lambda: session.calls >= 3)
    assert (
        await asyncio.wait_for(
            coordinator.async_run_local_admin_op("set", write, idempotent=False), 1.0
        )
        == "ok"
    )
    assert events == ["link closed", "written", "link closed"]
    calls = session.calls
    await _wait(lambda: session.calls > calls)  # the read goes on
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read


async def test_a_ble_unlock_does_not_wait_behind_a_long_wifi_read(
    coordinator, monkeypatch
):
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
    )
    monkeypatch.setattr(
        coordinator, "_ble_control_credentials", lambda: (None, b"", "0", 0, 0)
    )
    # No admin password for BLE, so the read goes over WiFi.
    monkeypatch.setattr(LockUpdateCoordinator, "admin_ps", property(lambda self: None))
    proxy = _wifi_proxy_stub(coordinator, monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    async def long_read(lock_id, op, on_sent):
        started.set()
        await release.wait()
        return (True, "done")

    proxy.async_admin_op.side_effect = long_read
    control = AsyncMock()
    monkeypatch.setattr(coord_mod, "async_control", control)

    read = asyncio.create_task(
        coordinator.async_run_local_admin_op(
            "read operate log", _walk, idempotent=True, read_only=True
        )
    )
    await started.wait()
    assert await asyncio.wait_for(coordinator.async_control_ble(unlock=True), 1.0)
    control.assert_awaited_once()

    release.set()
    assert await asyncio.wait_for(read, 1.0) == "done"


async def test_a_write_still_holds_the_link_for_the_whole_op(coordinator, monkeypatch):
    """Read-modify-write and non-idempotent writes are never interrupted."""
    events: list[str] = []
    session = _SlowPagedSession(events)
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    control = AsyncMock(side_effect=lambda *a, **k: events.append("unlocked"))
    monkeypatch.setattr(coord_mod, "async_control", control)

    async def three_pages(s) -> str:
        for _ in range(3):
            await s.command(0x36, b"")
        events.append("write done")
        return "ok"

    write = asyncio.create_task(
        coordinator.async_run_local_admin_op("set", three_pages, idempotent=False)
    )
    await _wait(lambda: session.calls >= 1)
    await asyncio.wait_for(coordinator.async_control_ble(unlock=True), 1.0)
    assert await write == "ok"
    assert events == ["write done", "unlocked"]


# --- saved settings read from the lock ----------------------------------------


def _settings_session(*, sound: bytes | None = None) -> _FakeAdminSession:
    """A lock with tamper alert on, passage auto-unlock off, 30 s and sound on."""
    return _FakeAdminSession(
        {
            # [batt][op 01][item 0x21][value 0x01]: tamper (bit 0) on, passage off
            0x68: _env(0x68, data=bytes.fromhex("3a010000002100000001")),
            0x36: _env(0x36, data=bytes.fromhex("3a01001e00010384")),
            0x62: sound or _env(0x62, data=bytes.fromhex("3a0101")),
        }
    )


def _poll_over_ble(monkeypatch, coordinator, session, *, cloud: bool) -> None:
    """A lock reachable over Bluetooth admin whose bolt reads locked."""
    _enable_ble_admin(monkeypatch, coordinator, session=session)
    coordinator._cloud_enabled_option = cloud
    coordinator.data.features = Features.tamper_alert | Features.passage_mode
    monkeypatch.setattr(
        coordinator,
        "async_read_lock_status",
        AsyncMock(return_value=LockStatus(battery=90, locked=True, door_open=None)),
    )


async def test_cloud_off_reads_the_saved_settings_from_the_lock(
    coordinator, monkeypatch
):
    session = _settings_session()
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)

    await coordinator.async_refresh()
    await coordinator.async_refresh()  # same slow interval: no second read

    assert [opcode for opcode, _ in session.calls] == [0x68, 0x36, 0x62]
    assert coordinator.last_update_success is True
    assert AutoLockDelay(coordinator).native_value == 30
    assert AutoLock(coordinator).is_on is True
    assert LockSound(coordinator).is_on is True
    assert TamperAlert(coordinator).is_on is True
    assert coordinator.data.passage_auto_unlock is False
    stored = await coordinator._store.async_get(coordinator.lock_id)
    assert stored["detail"] == {
        "autoLockTime": 30,
        "lockSound": 1,
        "tamperAlert": 1,
        "passageModeAutoUnlock": 2,
    }


async def test_the_first_settings_read_waits_for_a_local_route(
    coordinator, monkeypatch
):
    session = _settings_session()
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)
    monkeypatch.setattr(coordinator, "_ble_control_credentials", lambda: None)
    await coordinator.async_refresh()
    assert session.calls == []

    _fake_ble_credentials(monkeypatch, coordinator)
    await coordinator.async_refresh()
    assert len(session.calls) == 3


def test_an_unknown_on_off_in_the_detail_is_not_read_as_off(coordinator):
    coordinator._apply_detail(
        coordinator.data,
        LockDetail(lockSound=OnOff.unknown, tamperAlert=OnOff.off),
    )
    assert coordinator.data.lock_sound is None
    assert coordinator.data.tamper_alert is False


async def test_cloud_detail_supplies_the_settings_without_a_lock_read(
    coordinator, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    session = _settings_session()
    _poll_over_ble(monkeypatch, coordinator, session, cloud=True)

    await coordinator.async_refresh()

    assert session.calls == []
    assert coordinator.data.auto_lock_seconds == 60


async def test_a_failed_cloud_detail_falls_back_to_the_lock(
    coordinator, mock_api_responses, monkeypatch
):
    mock_api_responses("default")
    monkeypatch.setattr(
        TTLockApi, "get_lock", AsyncMock(side_effect=RequestFailed("down"))
    )
    session = _settings_session()
    _poll_over_ble(monkeypatch, coordinator, session, cloud=True)

    await coordinator.async_refresh()

    assert [opcode for opcode, _ in session.calls] == [0x68, 0x36, 0x62]
    assert coordinator.data.auto_lock_seconds == 30


async def test_a_failed_settings_read_keeps_the_previous_values(
    coordinator, monkeypatch
):
    session = _FakeAdminSession({0x68: _env(0x68, ok=False, data=b"\x02")})
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)
    coordinator.data.auto_lock_seconds = 10
    coordinator.data.lock_sound = False

    await coordinator.async_refresh()

    entity = AutoLockDelay(coordinator)
    assert session.calls == [(0x68, b"\x01")]
    assert coordinator.last_update_success is True
    assert entity.available is True
    assert entity.native_value == 10
    assert coordinator.data.lock_sound is False


async def test_a_settings_read_that_reads_nothing_is_retried_next_poll(
    coordinator, monkeypatch
):
    session = _FakeAdminSession({0x68: _env(0x68, ok=False, data=b"\x02")})
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)

    await coordinator.async_refresh()
    await coordinator.async_refresh()

    assert session.calls == [(0x68, b"\x01"), (0x68, b"\x01")]


async def test_a_refused_sound_read_keeps_the_settings_read_before_it(
    coordinator, monkeypatch
):
    session = _settings_session(sound=_env(0x62, ok=False, data=b"\x02"))
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)
    coordinator.data.lock_sound = False

    await coordinator.async_refresh()

    assert coordinator.data.auto_lock_seconds == 30
    assert coordinator.data.tamper_alert is True
    assert coordinator.data.lock_sound is False
    stored = await coordinator._store.async_get(coordinator.lock_id)
    assert "lockSound" not in stored["detail"]


async def test_an_unanswered_sound_read_keeps_the_rest_and_logs_quietly(
    coordinator, monkeypatch, caplog
):
    class _NoSoundAnswer(_FakeAdminSession):
        async def command(self, opcode: int, data: bytes = b"") -> bytes:
            if opcode == Command.AUDIO_MANAGE:
                raise BleNoResponse("no answer")
            return await super().command(opcode, data)

    session = _NoSoundAnswer(_settings_session()._replies)
    _poll_over_ble(monkeypatch, coordinator, session, cloud=False)
    coordinator.data.lock_sound = False

    with caplog.at_level(logging.DEBUG):
        await coordinator.async_refresh()

    assert coordinator.last_update_success is True
    assert coordinator.data.auto_lock_seconds == 30
    assert coordinator.data.tamper_alert is True
    assert coordinator.data.lock_sound is False
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_ready_takeover_reads_unknown_settings_soon(
    hass, coordinator, monkeypatch
):
    coordinator._cloud_enabled_option = False
    monkeypatch.setattr(
        LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
    )
    monkeypatch.setattr(
        LockUpdateCoordinator, "connectable", property(lambda self: True)
    )
    monkeypatch.setattr(coordinator, "wifi_credentials", lambda: CREDS)
    refresh = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh)
    coordinator._async_note_verified()  # the bolt is fresh

    coordinator.async_wifi_local_changed()  # settings unknown
    await hass.async_block_till_done()
    refresh.assert_awaited_once()

    coordinator.data.auto_lock_seconds = 5
    coordinator.data.lock_sound = True
    coordinator.async_wifi_local_changed()  # all known (no tamper or passage)
    await hass.async_block_till_done()
    refresh.assert_awaited_once()
