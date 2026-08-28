"""Tests for local Bluetooth presence tracking.

conftest's mock_bluetooth stands in for HA's bluetooth component - see its
docstring for why the real one can't be loaded here. Stubbing at exactly the
seam ble.py uses (three functions and a matcher) is what lets these assert
the things that are easy to get silently wrong: that the address is
normalised, that a lock heard before we loaded is picked up without waiting
for its next advertisement, that going out of range clears the reading
rather than freezing it, and that unsubscribing releases both registrations.
"""

from datetime import timedelta

from habluetooth import BluetoothScanningMode
import pytest

from custom_components.ttlock.ble import (
    BleData,
    async_bluetooth_available,
    async_track_address,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import MOCK_LOCK_MAC as LOCK_MAC

# BluetoothChange has exactly one member and ble.py ignores the argument
# entirely; this stands in for it.
ADVERTISEMENT = "advertisement"


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
        now = dt_util.utcnow()
        data = BleData(
            rssi=-80,
            last_seen=now - timedelta(seconds=222.6),
            source="local",
            connectable=True,
        )

        dumped = data.as_dict(now)

        assert dumped["rssi"] == -80
        assert dumped["source"] == "local"
        assert dumped["connectable"] is True
        assert dumped["age_seconds"] == pytest.approx(222.6)

    def test_a_lock_never_heard_reports_no_age(self):
        """None, not 0 - which would read as an advertisement received now."""
        assert BleData().as_dict(dt_util.utcnow())["age_seconds"] is None


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

    def test_lower_case_addresses_are_normalised(self, track, mock_bluetooth):
        """The cloud isn't consistent about case; a lower-case matcher matches nothing."""
        track(LOCK_MAC.lower())

        assert mock_bluetooth.matcher == {"address": LOCK_MAC, "connectable": False}
        assert mock_bluetooth.unavailable_address == LOCK_MAC
        assert mock_bluetooth.seed_address == LOCK_MAC

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
