"""Tests for local Bluetooth presence tracking and state reads.

conftest's mock_bluetooth stands in for HA's bluetooth component - see its
docstring for why the real one can't be loaded here. Stubbing at exactly the
seam ble.py uses (three functions and a matcher) is what lets these assert
the things that are easy to get silently wrong: that a lock heard before we
loaded is picked up without waiting for its next advertisement, that going
out of range clears the reading rather than freezing it, and that
unsubscribing releases both registrations.

mock_gatt does the same for the connection: it answers establish_connection
with a recorder that plays the lock's side of the GATT conversation.
"""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from bleak_retry_connector import BleakError
from habluetooth import BluetoothScanningMode
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock import ble
from custom_components.ttlock.api import RequestFailed
from custom_components.ttlock.ble import (
    BleConnectionError,
    BleData,
    BleError,
    BleNoResponse,
    BleNotInRange,
    async_bluetooth_available,
    async_command_session,
    async_read_status,
    async_track_address,
)
from custom_components.ttlock.ble_protocol import (
    NOTIFY_UUID,
    WRITE_UUID,
    ChecksumError,
    Command,
    Frame,
    LockVersion,
    parse_aes_key,
)
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_BLUETOOTH_ENABLED,
    DEFAULT_POLL_INTERVAL_MINUTES,
    DOMAIN,
)
from custom_components.ttlock.coordinator import RETRY_INTERVAL, LockUpdateCoordinator
from custom_components.ttlock.models import Lock, LockSummary
from custom_components.ttlock.store import LockStateStore
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .conftest import GattRecorder
from .const import (
    BASIC_LOCK_DETAILS,
    MOCK_LOCK_AES_KEY,
    MOCK_LOCK_MAC as LOCK_MAC,
    MOCK_LOCK_VERSION,
)

# BluetoothChange has exactly one member and ble.py ignores the argument
# entirely; this stands in for it.
ADVERTISEMENT = "advertisement"

LOCK_NAME = "Front Door"

POLL_INTERVAL = timedelta(minutes=DEFAULT_POLL_INTERVAL_MINUTES)

BLUETOOTH_OFF = {CONF_BLUETOOTH_ENABLED: False}


def _coordinator(
    hass: HomeAssistant, api, summary: LockSummary, options: dict | None = None
) -> LockUpdateCoordinator:
    config_entry = MockConfigEntry(domain=DOMAIN, options=options or {})
    config_entry.add_to_hass(hass)
    return LockUpdateCoordinator(
        hass, config_entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
    )


def _gatewayless(
    hass: HomeAssistant, api, options: dict | None = None
) -> LockUpdateCoordinator:
    """A coordinator for a lock the cloud has no route to."""
    summary = LockSummary(
        lockId=1, lockAlias="No Gateway", lockMac=LOCK_MAC, hasGateway=0
    )
    return _coordinator(hass, api, summary, options)


def _cloud_state_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    async def get_lock_state(self, lock_id):
        raise RequestFailed("no gateway")

    monkeypatch.setattr(
        "custom_components.ttlock.api.TTLockApi.get_lock_state", get_lock_state
    )


# A KT170's answer to QUERY_LOCK_STATUS: the echoed opcode, success, then
# battery 100%, locked, door closed.
STATUS_LOCKED = b"\x14\x01\x64\x00\x01"
STATUS_UNLOCKED_DOOR_OPEN = b"\x14\x01\x64\x01\x00"


@pytest.fixture
def lock_version() -> LockVersion:
    return LockVersion.from_cloud(MOCK_LOCK_VERSION)


@pytest.fixture
def aes_key() -> bytes:
    return parse_aes_key(MOCK_LOCK_AES_KEY)


@dataclass
class SpeakingLock:
    """A GattRecorder that answers commands the way a lock would.

    `answer` scripts the reply for one opcode (or every opcode, with
    `to=None`); what the lock was asked lands in `asked`.
    """

    gatt: GattRecorder
    version: LockVersion
    asked: list[Frame] = field(default_factory=list)
    _replies: dict[int | None, tuple[int, bytes, bytes | None, int]] = field(
        default_factory=dict
    )

    def answer(
        self,
        command: int = Command.RESPONSE,
        data: bytes = b"",
        key: bytes | None = None,
        chunk: int = 20,
        *,
        to: int | None = None,
    ) -> None:
        self._replies[to] = (command, data, key, chunk)

    def stay_silent(self) -> None:
        self._replies.clear()

    def respond(self, raw: bytes) -> list[bytes]:
        frame = Frame.decode(raw)
        self.asked.append(frame)
        reply = self._replies.get(frame.command, self._replies.get(None))
        if reply is None:
            return []
        command, data, key, chunk = reply
        wire = Frame(self.version, command, data).encode(aes_key=key)
        return [wire[i : i + chunk] for i in range(0, len(wire), chunk)]


@pytest.fixture
def speaking_lock(mock_gatt: GattRecorder, lock_version: LockVersion) -> SpeakingLock:
    lock = SpeakingLock(mock_gatt, lock_version)
    lock.answer()
    mock_gatt.responder = lock.respond
    return lock


class TestBleData:
    def test_never_heard_is_not_in_range(self):
        assert BleData().in_range is False

    def test_an_rssi_means_in_range(self):
        assert BleData(rssi=-62).in_range is True

    def test_zero_rssi_still_means_in_range(self):
        """0 dBm is implausibly strong but falsy - it must not read as absent."""
        assert BleData(rssi=0).in_range is True

    def test_a_dump_dates_the_advertisement_it_reports(self):
        """The payload and its age have to travel together.

        Read apart they have already produced a wrong answer: a payload
        emitted 222s before the operation it was taken to describe. Whoever
        reads a dump gets the age without having to know that.
        """
        data = BleData(
            rssi=-80,
            last_seen=dt_util.utcnow() - timedelta(seconds=222.6),
            source="local",
            connectable=True,
        )

        dumped = data.as_dict

        assert dumped["rssi"] == -80
        assert dumped["source"] == "local"
        assert dumped["connectable"] is True
        assert dumped["age_seconds"] == pytest.approx(222.6, abs=1)

    def test_a_lock_never_heard_reports_no_age(self):
        """None, not 0 - which would read as an advertisement received now."""
        assert BleData().as_dict["age_seconds"] is None


class TestAsyncBluetoothAvailable:
    async def test_false_when_component_is_not_set_up(self, hass: HomeAssistant):
        assert async_bluetooth_available(hass) is False

    async def test_true_once_component_is_set_up(self, hass: HomeAssistant):
        hass.config.components.add("bluetooth")
        assert async_bluetooth_available(hass) is True


class TestAsyncTrackAddress:
    @pytest.fixture
    def seen(self) -> list[BleData]:
        return []

    @pytest.fixture
    def track(self, hass: HomeAssistant, mock_bluetooth, seen):
        """Track LOCK_MAC, appending every reported change to `seen`."""

        def _track(address: str = LOCK_MAC):
            return async_track_address(hass, address, seen.append)

        return _track

    def test_matches_the_lock_by_address(self, track, mock_bluetooth):
        track()

        assert mock_bluetooth.matcher == {"address": LOCK_MAC, "connectable": False}
        assert mock_bluetooth.mode is BluetoothScanningMode.ACTIVE

    def test_watches_non_connectable_scanners_too(self, track, mock_bluetooth):
        """A passive-only sighting still answers "is the lock in range?"."""
        track()

        assert mock_bluetooth.unavailable_connectable is False

    def test_nothing_reported_when_lock_was_never_heard(self, track, seen):
        track()

        assert seen == []

    def test_seeds_from_the_last_known_advertisement(
        self, track, mock_bluetooth, ble_advertisement, seen
    ):
        """A lock advertising before we loaded shouldn't wait for the next packet."""
        mock_bluetooth.seed = ble_advertisement(rssi=-55, source="AA:BB:CC:DD:EE:FF")

        track()

        assert len(seen) == 1
        assert seen[0].rssi == -55
        assert seen[0].source == "AA:BB:CC:DD:EE:FF"
        assert seen[0].connectable is True
        assert seen[0].in_range is True

    def test_seeded_last_seen_reflects_the_advertisement_age(
        self, track, mock_bluetooth, ble_advertisement, seen
    ):
        """The seed can be minutes old - it must not be stamped as "now"."""
        stale = ble_advertisement()
        mock_bluetooth.seed = ble_advertisement(time=stale.time - 30)
        before = dt_util.utcnow()

        track()

        last_seen = seen[0].last_seen
        assert last_seen is not None
        assert 29 <= (before - last_seen).total_seconds() <= 31

    def test_advertisements_are_reported(
        self, track, mock_bluetooth, ble_advertisement, seen
    ):
        track()
        assert mock_bluetooth.advertisement is not None

        mock_bluetooth.advertisement(ble_advertisement(rssi=-71), ADVERTISEMENT)

        assert seen[-1].rssi == -71

    def test_going_out_of_range_clears_the_reading(
        self, track, mock_bluetooth, ble_advertisement, seen
    ):
        """A frozen RSSI would claim a lock is in range long after it isn't."""
        track()
        assert mock_bluetooth.advertisement is not None
        assert mock_bluetooth.unavailable is not None

        mock_bluetooth.advertisement(ble_advertisement(), ADVERTISEMENT)
        mock_bluetooth.unavailable(ble_advertisement())

        assert seen[-1] == BleData()
        assert seen[-1].in_range is False

    def test_unsubscribing_releases_both_registrations(self, track, mock_bluetooth):
        unsubscribe = track()

        unsubscribe()

        assert sorted(mock_bluetooth.unsubscribed) == ["advertisement", "unavailable"]


class TestCoordinatorBleTracking:
    """The coordinator's half - starting tracking, and coping without it."""

    async def test_starts_with_no_reading(self, coordinator):
        assert coordinator.ble == BleData()

    async def test_no_bluetooth_yields_a_working_no_op_unsubscribe(self, coordinator):
        """A cloud-only host must still set up - and tear down - cleanly."""
        unsubscribe = coordinator.async_start_ble_tracking()

        unsubscribe()

        assert coordinator.ble == BleData()

    async def test_tracks_the_lock_when_bluetooth_is_set_up(
        self, coordinator, mock_bluetooth, ble_advertisement
    ):
        mock_bluetooth.seed = ble_advertisement(address=coordinator.data.mac, rssi=-48)

        coordinator.async_start_ble_tracking()

        assert mock_bluetooth.matcher == {
            "address": coordinator.data.mac,
            "connectable": False,
        }
        assert coordinator.ble.rssi == -48

    async def test_advertisements_reach_entity_listeners(
        self, coordinator, mock_bluetooth, ble_advertisement
    ):
        """Entities only redraw if the coordinator notifies them."""
        coordinator.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None

        seen: list[int | None] = []
        remove = coordinator.async_add_listener(
            lambda: seen.append(coordinator.ble.rssi)
        )

        mock_bluetooth.advertisement(
            ble_advertisement(address=coordinator.data.mac, rssi=-66), ADVERTISEMENT
        )
        remove()

        assert seen == [-66]

    async def test_advertisements_leave_lock_state_alone(
        self, coordinator, mock_bluetooth, ble_advertisement
    ):
        """Advertisements say nothing about the lock, and must not stand in for a poll."""
        coordinator.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None
        before = coordinator.data

        mock_bluetooth.advertisement(
            ble_advertisement(address=coordinator.data.mac), ADVERTISEMENT
        )

        assert coordinator.data is before


class TestAsyncCommandSession:
    """Connecting and, above all, always disconnecting."""

    async def test_asks_for_a_connectable_route(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
            pass

        assert mock_bluetooth.device_address == LOCK_MAC
        assert mock_bluetooth.device_connectable is True
        assert speaking_lock.gatt.connected_name == LOCK_NAME
        assert speaking_lock.gatt.max_attempts == ble.CONNECT_ATTEMPTS

    async def test_no_route_is_not_in_range(
        self, hass, mock_bluetooth, mock_gatt, lock_version
    ):
        mock_bluetooth.device = None

        with pytest.raises(BleNotInRange, match=LOCK_NAME):
            async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
                pass

        assert mock_gatt.connects == 0

    async def test_disconnects_on_exit(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
            assert speaking_lock.gatt.disconnects == 0

        assert speaking_lock.gatt.disconnects == 1

    async def test_disconnects_when_the_body_raises(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        with pytest.raises(RuntimeError, match="body"):
            async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
                raise RuntimeError("body")

        assert speaking_lock.gatt.disconnects == 1

    async def test_a_failed_disconnect_does_not_mask_the_body(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        speaking_lock.gatt.disconnect_error = BleakError("gone")

        with pytest.raises(RuntimeError, match="body"):
            async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
                raise RuntimeError("body")

    async def test_a_failed_disconnect_alone_is_fine(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        speaking_lock.gatt.disconnect_error = BleakError("gone")

        async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
            pass

    async def test_a_refused_connection_names_the_lock(
        self, hass, mock_bluetooth, mock_gatt, lock_version
    ):
        mock_gatt.connect_error = BleakError("refused")

        with pytest.raises(BleConnectionError, match=f"{LOCK_NAME}.*refused"):
            async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
                pass

    async def test_gives_up_rather_than_hanging(
        self, hass, mock_bluetooth, mock_gatt, lock_version, monkeypatch
    ):
        monkeypatch.setattr(ble, "CONNECT_TIMEOUT", 0.01)
        mock_gatt.connect_delay = 5

        with pytest.raises(BleConnectionError, match="Timed out"):
            async with async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version):
                pass


class TestLockSession:
    """One command, one reply, over a subscribed connection."""

    @pytest.fixture
    def session(self, hass, mock_bluetooth, speaking_lock, lock_version):
        return async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version)

    async def test_subscribes_before_writing_and_unsubscribes_after(
        self, session, speaking_lock
    ):
        async with session as lock:
            assert speaking_lock.gatt.notify_uuid == NOTIFY_UUID
            assert speaking_lock.gatt.notify_stops == []
            await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

        assert speaking_lock.gatt.notify_stops == [NOTIFY_UUID]

    async def test_writes_to_the_command_characteristic(self, session, speaking_lock):
        async with session as lock:
            await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

        assert set(speaking_lock.gatt.written_uuids) == {WRITE_UUID}

    async def test_the_lock_receives_the_command(self, session, speaking_lock):
        async with session as lock:
            await lock.execute(Command.SEARCH_DEVICE_FEATURE, b"\x01", encrypted=False)

        (asked,) = speaking_lock.asked
        assert asked.command == Command.SEARCH_DEVICE_FEATURE
        assert asked.data == b"\x01"

    async def test_returns_the_reply(self, session, speaking_lock):
        speaking_lock.answer(Command.RESPONSE, b"\x01\x01\x02")

        async with session as lock:
            reply = await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

        assert reply.command == Command.RESPONSE
        assert reply.data == b"\x01\x01\x02"

    async def test_reassembles_a_reply_split_across_notifications(
        self, session, speaking_lock
    ):
        speaking_lock.answer(Command.RESPONSE, bytes(range(40)), chunk=7)

        async with session as lock:
            reply = await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

        assert reply.data == bytes(range(40))

    async def test_splits_a_long_command_to_the_mtu(self, session, speaking_lock):
        async with session as lock:
            await lock.execute(
                Command.SEARCH_DEVICE_FEATURE, bytes(60), encrypted=False
            )

        assert len(speaking_lock.gatt.writes) > 1
        assert all(len(write) <= 20 for write in speaking_lock.gatt.writes)

    async def test_uses_a_larger_negotiated_mtu(self, session, speaking_lock):
        speaking_lock.gatt.mtu_size = 247

        async with session as lock:
            await lock.execute(
                Command.SEARCH_DEVICE_FEATURE, bytes(60), encrypted=False
            )

        assert len(speaking_lock.gatt.writes) == 1

    async def test_an_unknown_mtu_means_the_default(self, session, speaking_lock):
        speaking_lock.gatt.mtu_size = None

        async with session as lock:
            await lock.execute(
                Command.SEARCH_DEVICE_FEATURE, bytes(60), encrypted=False
            )

        assert all(len(write) <= 20 for write in speaking_lock.gatt.writes)

    async def test_silence_is_no_response(self, session, speaking_lock, monkeypatch):
        monkeypatch.setattr(ble, "RESPONSE_TIMEOUT", 0.01)
        speaking_lock.stay_silent()

        async with session as lock:
            with pytest.raises(BleNoResponse, match=LOCK_NAME) as raised:
                await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

        assert not isinstance(raised.value, BleConnectionError)

    async def test_a_corrupt_reply_raises(self, session, speaking_lock, lock_version):
        wire = bytearray(Frame(lock_version, Command.RESPONSE, b"\x01\x01").encode())
        wire[12] ^= 0xFF
        speaking_lock.gatt.responder = lambda raw: [bytes(wire)]

        async with session as lock:
            with pytest.raises(ChecksumError):
                await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

    async def test_a_failed_write_is_a_connection_error(self, session, speaking_lock):
        speaking_lock.gatt.write_error = BleakError("dropped")

        async with session as lock:
            with pytest.raises(BleConnectionError, match="dropped"):
                await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)

    async def test_a_refused_subscription_is_a_connection_error(
        self, session, speaking_lock
    ):
        speaking_lock.gatt.notify_error = BleakError("no notify")

        with pytest.raises(BleConnectionError, match="subscribe"):
            async with session:
                pass

        assert speaking_lock.gatt.disconnects == 1

    async def test_unsubscribes_when_the_body_raises(self, session, speaking_lock):
        with pytest.raises(RuntimeError):
            async with session:
                raise RuntimeError("body")

        assert speaking_lock.gatt.notify_stops == [NOTIFY_UUID]

    async def test_a_failed_unsubscribe_is_fine(self, session, speaking_lock):
        async with session as lock:
            await lock.execute(Command.SEARCH_DEVICE_FEATURE, encrypted=False)
            speaking_lock.gatt.notify_error = BleakError("gone")


class TestLockSessionEncryption:
    @pytest.fixture
    def session(self, hass, mock_bluetooth, speaking_lock, lock_version, aes_key):
        return async_command_session(hass, LOCK_MAC, LOCK_NAME, lock_version, aes_key)

    async def test_the_payload_is_encrypted_on_the_wire(
        self, session, speaking_lock, aes_key
    ):
        async with session as lock:
            await lock.execute(Command.QUERY_LOCK_STATUS, b"hello")

        (raw,) = speaking_lock.gatt.received
        assert Frame.decode(raw).data != b"hello"
        assert Frame.decode(raw, aes_key=aes_key).data == b"hello"

    async def test_an_encrypted_reply_is_decrypted(
        self, session, speaking_lock, aes_key
    ):
        speaking_lock.answer(Command.RESPONSE, STATUS_LOCKED, key=aes_key)

        async with session as lock:
            reply = await lock.execute(Command.QUERY_LOCK_STATUS)

        assert reply.data == STATUS_LOCKED

    async def test_a_cleartext_command_stays_cleartext(self, session, speaking_lock):
        async with session as lock:
            await lock.execute(Command.SEARCH_DEVICE_FEATURE, b"\x01", encrypted=False)

        (raw,) = speaking_lock.gatt.received
        assert Frame.decode(raw).data == b"\x01"

    async def test_no_key_refuses_to_send(
        self, hass, mock_bluetooth, speaking_lock, lock_version
    ):
        async with async_command_session(
            hass, LOCK_MAC, LOCK_NAME, lock_version
        ) as lock:
            with pytest.raises(BleError, match="no AES key"):
                await lock.execute(Command.QUERY_LOCK_STATUS)

        assert speaking_lock.gatt.writes == []


class TestAsyncReadStatus:
    @pytest.fixture(autouse=True)
    def locked(self, speaking_lock, aes_key):
        speaking_lock.answer(Command.RESPONSE, STATUS_LOCKED, key=aes_key)

    async def test_reads_battery_lock_and_door(
        self, hass, mock_bluetooth, lock_version, aes_key
    ):
        status = await async_read_status(
            hass, LOCK_MAC, LOCK_NAME, lock_version, aes_key
        )

        assert (status.battery, status.locked, status.door_open) == (100, True, False)

    async def test_asks_with_an_encrypted_query(
        self, hass, mock_bluetooth, speaking_lock, lock_version, aes_key
    ):
        await async_read_status(hass, LOCK_MAC, LOCK_NAME, lock_version, aes_key)

        (asked,) = speaking_lock.asked
        assert asked.command == Command.QUERY_LOCK_STATUS
        (raw,) = speaking_lock.gatt.received
        assert Frame.decode(raw, aes_key=aes_key).data == b""

    async def test_closes_the_connection(
        self, hass, mock_bluetooth, speaking_lock, lock_version, aes_key
    ):
        await async_read_status(hass, LOCK_MAC, LOCK_NAME, lock_version, aes_key)

        assert speaking_lock.gatt.notify_stops == [NOTIFY_UUID]
        assert speaking_lock.gatt.disconnects == 1

    async def test_a_short_reply_reads_as_unknown(
        self, hass, mock_bluetooth, speaking_lock, lock_version, aes_key
    ):
        speaking_lock.answer(Command.RESPONSE, b"\x14\x01", key=aes_key)

        status = await async_read_status(
            hass, LOCK_MAC, LOCK_NAME, lock_version, aes_key
        )

        assert (status.battery, status.locked, status.door_open) == (None, None, None)


class TestCoordinatorBleRead:
    """The poll's fast tier: local when it can be, cloud otherwise.

    The default cloud scenario reports the lock unlocked; the speaking lock
    reports it locked, so which one answered is visible in the result.
    """

    @pytest.fixture
    def in_range(self, coordinator, mock_bluetooth):
        coordinator.ble = BleData(rssi=-60, connectable=True)

    @pytest.fixture
    def cloud_unreachable(self, monkeypatch):
        _cloud_state_fails(monkeypatch)

    @pytest.fixture
    def locked_locally(self, speaking_lock, aes_key):
        speaking_lock.answer(Command.RESPONSE, STATUS_LOCKED, key=aes_key)
        return speaking_lock

    async def test_without_bluetooth_the_cloud_answers(
        self, coordinator, mock_api_responses
    ):
        mock_api_responses("default")

        await coordinator.async_refresh()

        assert coordinator.data.locked is False

    async def test_in_range_the_lock_answers(
        self,
        coordinator,
        mock_api_responses,
        in_range,
        locked_locally,
        cloud_unreachable,
    ):
        mock_api_responses("default")

        await coordinator.async_refresh()

        assert coordinator.last_update_success
        assert coordinator.data.locked is True
        assert coordinator.data.battery_level == 100
        assert locked_locally.gatt.connects == 1

    async def test_out_of_range_the_cloud_answers(
        self, coordinator, mock_api_responses, mock_bluetooth, locked_locally
    ):
        mock_api_responses("default")

        await coordinator.async_refresh()

        assert coordinator.data.locked is False
        assert locked_locally.gatt.connects == 0

    async def test_an_unreadable_key_means_the_cloud(
        self, coordinator, mock_api_responses, in_range, locked_locally, monkeypatch
    ):
        mock_api_responses("default")

        async def get_lock(self, lock_id):
            return Lock.model_validate({**BASIC_LOCK_DETAILS, "aesKeyStr": "<REMOVED>"})

        monkeypatch.setattr("custom_components.ttlock.api.TTLockApi.get_lock", get_lock)

        await coordinator.async_refresh()

        assert coordinator.data.locked is False
        assert locked_locally.gatt.connects == 0

    async def test_a_failed_local_read_falls_back_to_the_cloud(
        self, coordinator, mock_api_responses, in_range, locked_locally
    ):
        mock_api_responses("default")
        locked_locally.gatt.connect_error = BleakError("refused")

        await coordinator.async_refresh()

        assert coordinator.last_update_success
        assert coordinator.data.locked is False

    async def test_a_silent_lock_falls_back_to_the_cloud(
        self, coordinator, mock_api_responses, in_range, locked_locally, monkeypatch
    ):
        mock_api_responses("default")
        monkeypatch.setattr(ble, "RESPONSE_TIMEOUT", 0.01)
        locked_locally.stay_silent()

        await coordinator.async_refresh()

        assert coordinator.last_update_success
        assert coordinator.data.locked is False

    async def test_door_state_reaches_a_present_sensor(
        self, coordinator, mock_api_responses, in_range, locked_locally, aes_key
    ):
        mock_api_responses("with_sensor")
        locked_locally.answer(Command.RESPONSE, STATUS_UNLOCKED_DOOR_OPEN, key=aes_key)

        await coordinator.async_refresh()

        assert coordinator.data.locked is False
        assert coordinator.data.sensor is not None
        assert coordinator.data.sensor.opened is True

    async def test_door_state_is_ignored_without_a_sensor(
        self, coordinator, mock_api_responses, in_range, locked_locally, aes_key
    ):
        mock_api_responses("default")
        locked_locally.answer(Command.RESPONSE, STATUS_UNLOCKED_DOOR_OPEN, key=aes_key)

        await coordinator.async_refresh()

        assert coordinator.data.locked is False
        assert coordinator.data.sensor is None


class TestBluetoothDisabled:
    """Turning the option off leaves a lock reachable over the cloud only."""

    @pytest.fixture
    def cloud_backed(self, hass, api) -> LockUpdateCoordinator:
        """A lock with a gateway, Bluetooth switched off for its entry."""
        summary = LockSummary(
            lockId=7252408,
            lockAlias="Test Lock",
            lockMac="00:00:00:00:00:00",
            hasGateway=1,
        )
        return _coordinator(hass, api, summary, BLUETOOTH_OFF)

    async def test_the_radio_is_on_by_default(self, hass, api, mock_bluetooth):
        assert _gatewayless(hass, api).ble_enabled is True

    async def test_the_option_alone_does_not_turn_the_radio_on(self, hass, api):
        assert _gatewayless(hass, api).ble_enabled is False

    async def test_a_disabled_radio_is_not_tracked(self, hass, api, mock_bluetooth):
        unsubscribe = _gatewayless(hass, api, BLUETOOTH_OFF).async_start_ble_tracking()
        unsubscribe()

        assert mock_bluetooth.advertisement is None
        assert mock_bluetooth.unsubscribed == []

    async def test_a_disabled_radio_is_not_reachable(self, hass, api, mock_bluetooth):
        gatewayless = _gatewayless(hass, api, BLUETOOTH_OFF)
        gatewayless.ble = BleData(rssi=-60, connectable=True)

        assert gatewayless.ble_reachable is False
        assert gatewayless.connectable is False
        assert gatewayless.update_interval is None

    async def test_a_disabled_radio_leaves_the_cloud_to_answer(
        self, cloud_backed, mock_api_responses, mock_bluetooth, speaking_lock, aes_key
    ):
        speaking_lock.answer(Command.RESPONSE, STATUS_LOCKED, key=aes_key)
        cloud_backed.ble = BleData(rssi=-60, connectable=True)
        mock_api_responses("default")

        await cloud_backed.async_refresh()

        assert cloud_backed.data.locked is False
        assert speaking_lock.gatt.connects == 0

    async def test_a_disabled_radio_gets_no_grace(
        self, cloud_backed, mock_api_responses, mock_bluetooth, monkeypatch
    ):
        mock_api_responses("default")
        await cloud_backed.async_refresh()
        cloud_backed.ble = BleData(rssi=-60, connectable=True)
        _cloud_state_fails(monkeypatch)

        await cloud_backed.async_refresh()

        assert cloud_backed.last_update_success is False
        assert cloud_backed.update_interval == POLL_INTERVAL


class TestCoordinatorReachability:
    """A lock only the radio can reach is polled for as long as the radio hears it."""

    @pytest.fixture
    def gatewayless(self, hass, api) -> LockUpdateCoordinator:
        return _gatewayless(hass, api)

    @pytest.fixture
    def refresh_requests(self, monkeypatch):
        """Stub a coordinator's async_request_refresh, returning the mock."""

        def _stub(coordinator: LockUpdateCoordinator) -> AsyncMock:
            requested = AsyncMock()
            monkeypatch.setattr(coordinator, "async_request_refresh", requested)
            return requested

        return _stub

    async def test_a_gatewayless_lock_starts_unreachable(self, gatewayless):
        assert gatewayless.cloud_connectable is False
        assert gatewayless.connectable is False
        assert gatewayless.update_interval is None

    async def test_radio_range_is_enough_on_its_own(self, gatewayless):
        gatewayless.ble = BleData(rssi=-60, connectable=True)

        assert gatewayless.ble_reachable is True
        assert gatewayless.connectable is True

    async def test_a_passive_sighting_is_not_reachability(self, gatewayless):
        """A passive-only scanner hears the lock but can't connect to it."""
        gatewayless.ble = BleData(rssi=-60, connectable=False)

        assert gatewayless.connectable is False

    async def test_a_cloud_reachable_lock_does_not_need_the_radio(self, coordinator):
        assert coordinator.ble_reachable is False
        assert coordinator.connectable is True

    async def test_coming_into_range_starts_the_poll(
        self, hass, gatewayless, refresh_requests, mock_bluetooth, ble_advertisement
    ):
        requested = refresh_requests(gatewayless)
        gatewayless.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None

        mock_bluetooth.advertisement(ble_advertisement(), ADVERTISEMENT)
        await hass.async_block_till_done()

        assert gatewayless.connectable is True
        assert gatewayless.update_interval == POLL_INTERVAL
        requested.assert_awaited_once()

    async def test_going_out_of_range_stops_the_poll(
        self, hass, gatewayless, refresh_requests, mock_bluetooth, ble_advertisement
    ):
        refresh_requests(gatewayless)
        gatewayless.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None
        assert mock_bluetooth.unavailable is not None
        mock_bluetooth.advertisement(ble_advertisement(), ADVERTISEMENT)

        mock_bluetooth.unavailable(ble_advertisement())
        await hass.async_block_till_done()

        assert gatewayless.connectable is False
        assert gatewayless.update_interval is None

    async def test_only_the_edge_schedules_anything(
        self, hass, gatewayless, refresh_requests, mock_bluetooth, ble_advertisement
    ):
        """Advertisements arrive every couple of seconds; polls must not."""
        requested = refresh_requests(gatewayless)
        gatewayless.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None

        for _ in range(10):
            mock_bluetooth.advertisement(ble_advertisement(), ADVERTISEMENT)
        await hass.async_block_till_done()

        requested.assert_awaited_once()

    async def test_a_cloud_reachable_lock_has_its_poll_left_alone(
        self, hass, coordinator, refresh_requests, mock_bluetooth, ble_advertisement
    ):
        requested = refresh_requests(coordinator)
        coordinator.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None
        assert mock_bluetooth.unavailable is not None
        heard = ble_advertisement(address=coordinator.data.mac)

        mock_bluetooth.advertisement(heard, ADVERTISEMENT)
        mock_bluetooth.unavailable(heard)
        await hass.async_block_till_done()

        assert coordinator.update_interval == POLL_INTERVAL
        requested.assert_not_awaited()

    async def test_an_unreachable_lock_spends_no_api_call(
        self, gatewayless, monkeypatch
    ):
        get_lock = AsyncMock()
        monkeypatch.setattr(gatewayless.api, "get_lock", get_lock)

        await gatewayless.async_refresh()

        assert gatewayless.last_update_success is False
        get_lock.assert_not_awaited()

    async def test_diagnostics_show_both_halves(self, gatewayless):
        gatewayless.ble = BleData(rssi=-60, connectable=True)

        dumped = gatewayless.as_dict()

        assert dumped["connectable"] is True
        assert dumped["cloud_connectable"] is False
        assert dumped["ble_reachable"] is True
        assert dumped["ble_proven"] is False


class TestCoordinatorBleProof:
    """A read that worked vouches for the lock after its advertisements stop.

    TTLock locks advertise in bursts minutes apart; without this a lock that
    answers every poll would flap unavailable between bursts.
    """

    @pytest.fixture
    def locked_locally(self, speaking_lock, aes_key):
        speaking_lock.answer(Command.RESPONSE, STATUS_LOCKED, key=aes_key)
        return speaking_lock

    @pytest.fixture
    async def read_locally(self, coordinator, mock_api_responses, locked_locally):
        """The coordinator, having just read the lock over Bluetooth."""
        mock_api_responses("default")
        coordinator.ble = BleData(rssi=-60, connectable=True)
        await coordinator.async_refresh()
        assert coordinator.data.locked is True
        assert locked_locally.gatt.connects == 1
        return coordinator

    async def test_a_read_that_worked_outlasts_the_advertisements(self, read_locally):
        read_locally.ble = BleData()

        assert read_locally.ble_proven is True
        assert read_locally.ble_reachable is True

    async def test_a_quiet_lock_that_answered_is_still_asked(
        self, read_locally, locked_locally
    ):
        """The read path honours the proof too - else every poll falls to the cloud."""
        read_locally.ble = BleData()

        await read_locally.async_refresh()

        assert locked_locally.gatt.connects == 2
        assert read_locally.data.locked is True

    async def test_the_proof_outlives_the_poll(self, read_locally):
        """Shorter, and a healthy lock would flap while waiting for its next poll."""
        later = dt_util.utcnow() + POLL_INTERVAL + timedelta(minutes=1)

        with patch.object(dt_util, "utcnow", return_value=later):
            assert read_locally.ble_proven is True

    async def test_the_proof_expires(self, read_locally):
        later = dt_util.utcnow() + 2 * POLL_INTERVAL + timedelta(minutes=1)

        with patch.object(dt_util, "utcnow", return_value=later):
            assert read_locally.ble_proven is False

    async def test_a_read_that_failed_proves_nothing(
        self, read_locally, locked_locally
    ):
        locked_locally.gatt.connect_error = BleakError("refused")

        await read_locally.async_refresh()
        read_locally.ble = BleData()

        assert read_locally.data.locked is False  # the cloud answered
        assert read_locally.ble_proven is False

    async def test_a_local_read_ends_the_retry_cadence(
        self, read_locally, locked_locally, monkeypatch
    ):
        _cloud_state_fails(monkeypatch)
        locked_locally.gatt.connect_error = BleakError("refused")
        await read_locally.async_refresh()
        assert read_locally.last_update_success is True
        assert read_locally.update_interval == RETRY_INTERVAL

        locked_locally.gatt.connect_error = None
        await read_locally.async_refresh()

        assert read_locally.update_interval == POLL_INTERVAL

    async def test_an_advertisement_does_not_slow_down_a_recovery(
        self,
        hass,
        read_locally,
        locked_locally,
        mock_bluetooth,
        ble_advertisement,
        monkeypatch,
    ):
        """A lock still reachable stays on the retry cadence when it advertises."""
        read_locally.async_start_ble_tracking()
        assert mock_bluetooth.advertisement is not None
        _cloud_state_fails(monkeypatch)
        locked_locally.gatt.connect_error = BleakError("refused")
        await read_locally.async_refresh()
        assert read_locally.update_interval == RETRY_INTERVAL

        mock_bluetooth.advertisement(
            ble_advertisement(address=read_locally.data.mac), ADVERTISEMENT
        )
        await hass.async_block_till_done()

        assert read_locally.update_interval == RETRY_INTERVAL
