"""Test ttlock setup process."""

import asyncio
from datetime import datetime, timedelta
import logging
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, Mock, PropertyMock, patch

import dateparser
import pytest
from pytest_homeassistant.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.ttlock import coordinator as coord_mod
from custom_components.ttlock.api import RequestFailed, TTLockApi
from custom_components.ttlock.ble import BleData, BleError
from custom_components.ttlock.ble_protocol import LockStatus
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import (
    CONF_BLE_CONTROL,
    CONF_BLUETOOTH_ENABLED,
    CONF_POLL_INTERVAL,
    CONF_SLOW_POLL_INTERVAL,
    DOMAIN,
    TRANSPORT_BLE,
    TRANSPORT_WIFI_LOCAL,
    TT_GATEWAYS,
    TT_STORE,
    TT_WIFI_PROXY,
)
from custom_components.ttlock.coordinator import (
    LOCAL_REFRESH_INTERVAL,
    RETRY_INTERVAL,
    REVERIFY_GRACE,
    CredentialInfo,
    CredentialNames,
    GatewaysUpdateCoordinator,
    LockState,
    LockUpdateCoordinator,
    NoLocalAdminTransport,
    SensorData,
    async_add_when_sensor_present,
    door_state_known,
)
from custom_components.ttlock.models import (
    AccessLevel,
    Features,
    Gateway,
    GatewayLink,
    Lock,
    LockState as WireLockState,
    LockSummary,
    PassageModeConfig,
    WebhookEvent,
)
from custom_components.ttlock.store import LockStateStore
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BASIC_LOCK_DETAILS,
    LOCK_STATE_LOCKED,
    LOCK_STATE_UNKNOWN,
    LOCK_STATE_UNLOCKED,
    MOCK_LOCK_AES_KEY,
    MOCK_LOCK_VERSION,
    PASSAGE_MODE_6_TO_6_7_DAYS,
    PASSAGE_MODE_ALL_DAY_WEEKDAYS,
    WEBHOOK_LOCK_10AM_UTC,
    WEBHOOK_SENSOR_CLOSE,
    WEBHOOK_SENSOR_OPEN,
    WEBHOOK_UNLOCK_10AM_UTC,
)
from .test_lock_commands import LogLock
from .test_services import _cloud_off, _lock, _log_locally


@pytest.fixture(params=[{"auto_lock_seconds": 30}])
def lock_state(request):
    return LockState(
        name="Test Lock",
        mac="00:00:00:00:00:00",
        auto_lock_seconds=request.param["auto_lock_seconds"],
    )


def ts(time: str = "now"):
    return dateparser.parse(time)


class TestLockState:
    class TestPassageModeActive:
        @pytest.mark.parametrize(
            "time",
            [
                "6am on Sunday",
                "10am on Wednesday",
                "5:59pm on Friday",
            ],
        )
        def test_is_true_during_set_passage_mode_times(self, lock_state, time):
            lock_state.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )
            assert lock_state.passage_mode_active(ts(time)) is True

        @pytest.mark.parametrize(
            "time",
            [
                "Midnight on Monday",
                "5:59am on Tuesday",
                "6pm on Thursday",
                "11:59pm on Sunday",
            ],
        )
        def test_is_false_outside_set_passage_mode_times(self, lock_state, time):
            lock_state.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )
            assert lock_state.passage_mode_active(ts(time)) is False

    class TestAutoLockDelay:
        @pytest.mark.parametrize(
            "lock_state", [{"auto_lock_seconds": -1}], indirect=True
        )
        def test_is_none_without_config(self, lock_state):
            assert lock_state.auto_lock_delay(ts()) is None

        def test_is_auto_lock_without_passage_mode(self, lock_state):
            assert lock_state.auto_lock_delay(ts()) == lock_state.auto_lock_seconds

        @pytest.mark.parametrize(
            "time",
            [
                "Midnight on Monday",
                "5:59am on Tuesday",
                "6pm on Thursday",
                "11:59pm on Sunday",
            ],
        )
        def test_is_auto_lock_outside_set_passage_mode_times(self, lock_state, time):
            lock_state.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )
            assert lock_state.auto_lock_delay(ts(time)) == lock_state.auto_lock_seconds

        @pytest.mark.parametrize(
            "time",
            [
                "6am on Sunday",
                "10am on Wednesday",
                "5:59pm on Friday",
            ],
        )
        def test_is_none_during_set_passage_mode_times(self, lock_state, time):
            lock_state.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )
            assert lock_state.auto_lock_delay(ts(time)) is None

        @pytest.mark.parametrize(
            "time",
            [
                "1am on Monday",
                "3pm on Thursday",
                "11:30pm on Friday",
            ],
        )
        def test_is_none_when_passage_mode_is_all_day(self, lock_state, time):
            lock_state.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_ALL_DAY_WEEKDAYS
            )
            assert lock_state.auto_lock_delay(ts(time)) is None


class TestAsyncAddWhenSensorPresent:
    """coordinator.py's __init__.py always calls this before a lock's first
    refresh has run (see async_add_when_sensor_present's own docstring), so
    the "already present" branch is never exercised through the real
    component_setup flow - test it directly as a unit instead.
    """

    async def test_adds_immediately_when_already_present(
        self, coordinator: LockUpdateCoordinator
    ):
        coordinator.data.sensor = SensorData(battery=50)
        calls = []

        async_add_when_sensor_present(coordinator, lambda: calls.append(True))

        assert calls == [True]

    async def test_defers_until_presence_confirmed(
        self, coordinator: LockUpdateCoordinator
    ):
        calls = []

        async_add_when_sensor_present(coordinator, lambda: calls.append(True))
        assert calls == []

        coordinator.data.sensor = SensorData(battery=50)
        coordinator.async_update_listeners()

        assert calls == [True]

    async def test_a_door_state_alone_is_enough_for_the_door_entity(
        self, coordinator: LockUpdateCoordinator
    ):
        """A lock with a built-in contact reports the door and has no
        accessory, so the door entity waits for the state, not the accessory.
        """
        calls = []

        async_add_when_sensor_present(
            coordinator, lambda: calls.append(True), ready=door_state_known
        )
        coordinator.data.sensor = SensorData(opened=True)
        coordinator.async_update_listeners()

        assert calls == [True]

    async def test_a_door_state_alone_does_not_add_the_accessory_battery(
        self, coordinator: LockUpdateCoordinator
    ):
        """There is no accessory to report a battery for."""
        calls = []

        async_add_when_sensor_present(coordinator, lambda: calls.append(True))
        coordinator.data.sensor = SensorData(opened=True)
        coordinator.async_update_listeners()

        assert calls == []

    async def test_listener_is_removed_after_firing(
        self, coordinator: LockUpdateCoordinator
    ):
        calls = []

        async_add_when_sensor_present(coordinator, lambda: calls.append(True))

        coordinator.data.sensor = SensorData(battery=50)
        coordinator.async_update_listeners()
        coordinator.async_update_listeners()

        assert calls == [True]


class TestLockUpdateCoordinator:
    class TestAsyncRefresh:
        async def test_coordinator_loads_data(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()

            assert coordinator.data.name == BASIC_LOCK_DETAILS["lockAlias"]
            assert coordinator.data.locked is False
            assert coordinator.data.sensor is None
            assert coordinator.data.action_pending is False
            assert coordinator.data.last_user is None
            assert coordinator.data.last_reason is None
            assert coordinator.data.features is not None

        async def test_coordinator_loads_sensor_data(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.opened is False
            assert coordinator.data.sensor.battery == 85
            assert coordinator.data.sensor.last_fetched is not None
            assert coordinator.data.sensor.last_fetched > dt_util.now() - timedelta(
                seconds=3
            )

        async def test_a_cloud_read_refreshes_the_battery(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            """queryOpenState reads the battery at the same moment as the
            bolt, where lock/detail's copy is as old as the lock's last
            check-in.
            """
            mock_api_responses("default")
            await coordinator.async_refresh()
            assert coordinator.data.battery_level == 90

            async def mock_get_lock_state_half_empty(*args, **kwargs):
                return WireLockState.model_validate(
                    {**LOCK_STATE_UNLOCKED, "electricQuantity": 55}
                )

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_half_empty,
            )

            await coordinator.async_refresh()
            assert coordinator.data.battery_level == 55

        async def test_a_cloud_unknown_does_not_claim_the_door_is_closed(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            """queryOpenState omits sensorState when the lock can't read its
            door; the last known state beats inventing a closed one.
            """
            mock_api_responses("sensor_state_unknown")
            await coordinator.async_refresh()
            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.opened is None

            coordinator.data.sensor.opened = True
            await coordinator.async_refresh()

            assert coordinator.data.sensor.opened is True

        async def test_a_cloud_door_state_needs_no_accessory(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            """A lock with a built-in door contact reports the door in
            queryOpenState and has no accessory for doorSensor/query to
            describe, so requiring one threw that door state away.
            """
            mock_api_responses("door_without_accessory")
            await coordinator.async_refresh()

            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.present is False
            assert coordinator.data.sensor.opened is False

        async def test_coordinator_handles_missing_sensor(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("sensor_not_installed")
            await coordinator.async_refresh()

            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.present is False
            assert coordinator.data.sensor.last_fetched is not None
            assert coordinator.data.sensor.last_fetched > dt_util.now() - timedelta(
                seconds=3
            )

        async def test_sensor_data_only_fetched_once(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")

            await coordinator.async_refresh()
            assert coordinator.data.sensor is not None
            t0 = coordinator.data.sensor.last_fetched

            await coordinator.async_refresh()
            assert coordinator.data.sensor is not None
            t1 = coordinator.data.sensor.last_fetched

            assert t0 == t1

        async def test_present_sensor_battery_refetched_after_a_day(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")

            await coordinator.async_refresh()
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.last_fetched = dt_util.now() - timedelta(days=2)

            await coordinator.async_refresh()

            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.last_fetched > dt_util.now() - timedelta(
                seconds=3
            )

        async def test_locked_state_reverified_every_refresh(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            assert coordinator.data.locked is False

            async def mock_get_lock_state_locked(*args, **kwargs):
                return WireLockState.model_validate(LOCK_STATE_LOCKED)

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_locked,
            )

            await coordinator.async_refresh()
            assert coordinator.data.locked is True

        async def test_a_cloud_unknown_does_not_claim_the_lock_is_unlocked(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            """queryOpenState answers `2` for a lock it could not reach.

            A bolt nobody could read is neither locked nor unlocked, and the
            lock entity says so instead of picking one.
            """
            mock_api_responses("locked")
            await coordinator.async_refresh()
            assert coordinator.data.locked is True

            async def mock_get_lock_state_unknown(*args, **kwargs):
                return WireLockState.model_validate(LOCK_STATE_UNKNOWN)

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_unknown,
            )

            await coordinator.async_refresh()
            assert coordinator.data.locked is None

        async def test_a_lock_that_only_ever_answers_unknown_stays_unknown(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("state_unknown")
            await coordinator.async_refresh()

            assert coordinator.data.locked is None

        async def test_a_lock_that_answered_unknown_still_goes_unavailable(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            """An unknown answer is still an answer.

            Not being able to read the lock at all is a different thing from
            the lock telling us it doesn't know, and keeps the unavailable
            path that a published `None` can no longer stand in for.
            """
            mock_api_responses("state_unknown")
            await coordinator.async_refresh()
            assert coordinator.data.locked is None
            assert coordinator.last_update_success is True

            async def mock_get_lock_state_fails(*args, **kwargs):
                raise RequestFailed("boom")

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_fails,
            )

            await coordinator.async_refresh()

            assert coordinator.last_update_success is False

        async def test_first_lock_state_fetch_failure_tolerated(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")

            async def mock_get_lock_state_fails(*args, **kwargs):
                raise RequestFailed("boom")

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_fails,
            )

            await coordinator.async_refresh()

            assert coordinator.data.locked is None
            assert coordinator.last_update_success is True

        async def test_lock_state_reverification_failure_marks_unavailable(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            assert coordinator.data.locked is False
            assert coordinator.last_update_success is True

            async def mock_get_lock_state_fails(*args, **kwargs):
                raise RequestFailed("boom")

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                mock_get_lock_state_fails,
            )

            await coordinator.async_refresh()

            assert coordinator.last_update_success is False

        async def test_non_connectable_lock_never_polled(self, hass, api):
            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=1,
                lockAlias="No Gateway",
                lockMac="00:00:00:00:00:01",
                hasGateway=0,
                userType="110301",
            )
            coordinator = LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )

            assert coordinator.connectable is False
            assert coordinator.update_interval is None

            await coordinator.async_refresh()

            assert coordinator.last_update_success is False

    class TestReverifyGrace:
        """A lock the radio can hear rides out a couple of failed state reads.

        One connection that didn't take says nothing about the bolt; a lock
        nothing can hear still goes unavailable on the first failure.
        """

        @pytest.fixture
        async def verified(self, coordinator, mock_api_responses):
            """The coordinator, holding a state it has verified once."""
            mock_api_responses("default")
            await coordinator.async_refresh()
            assert coordinator.data.locked is False
            return coordinator

        @pytest.fixture
        def cloud_state(self, monkeypatch):
            """Control whether lock/queryOpenState answers; returns the switch."""
            failing = False

            async def get_lock_state(self, lock_id):
                if failing:
                    raise RequestFailed("boom")
                return WireLockState.model_validate(LOCK_STATE_LOCKED)

            def _set(fails: bool) -> None:
                nonlocal failing
                failing = fails

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_lock_state",
                get_lock_state,
            )
            return _set

        async def test_an_audible_lock_rides_out_one_failed_read(
            self, verified, cloud_state
        ):
            verified.ble = BleData(rssi=-60, connectable=True)
            cloud_state(fails=True)

            await verified.async_refresh()

            assert verified.last_update_success is True
            assert verified.data.locked is False
            assert verified.update_interval == RETRY_INTERVAL

        async def test_the_grace_runs_out(self, verified, cloud_state):
            verified.ble = BleData(rssi=-60, connectable=True)
            cloud_state(fails=True)

            for _ in range(REVERIFY_GRACE):
                await verified.async_refresh()
                assert verified.last_update_success is True
            await verified.async_refresh()

            assert verified.last_update_success is False
            assert verified.update_interval == verified._poll_interval

        async def test_a_lock_that_stays_silent_is_not_retried_forever(
            self, verified, cloud_state
        ):
            """Past the grace every poll is a failed GATT session; keep them rare."""
            verified.ble = BleData(rssi=-60, connectable=True)
            cloud_state(fails=True)
            for _ in range(REVERIFY_GRACE):
                await verified.async_refresh()

            for _ in range(3):
                await verified.async_refresh()
                assert verified.last_update_success is False
                assert verified.update_interval == verified._poll_interval

        async def test_recovery_after_the_grace_ran_out(self, verified, cloud_state):
            verified.ble = BleData(rssi=-60, connectable=True)
            cloud_state(fails=True)
            for _ in range(REVERIFY_GRACE + 1):
                await verified.async_refresh()

            cloud_state(fails=False)
            await verified.async_refresh()

            assert verified.last_update_success is True
            assert verified._reverify_failures == 0
            assert verified.update_interval == verified._poll_interval

        async def test_one_good_read_resets_the_grace(self, verified, cloud_state):
            verified.ble = BleData(rssi=-60, connectable=True)
            cloud_state(fails=True)
            for _ in range(REVERIFY_GRACE):
                await verified.async_refresh()

            cloud_state(fails=False)
            await verified.async_refresh()
            assert verified.update_interval == timedelta(minutes=30)

            cloud_state(fails=True)
            for _ in range(REVERIFY_GRACE):
                await verified.async_refresh()
                assert verified.last_update_success is True

        async def test_a_silent_lock_gets_no_grace(self, verified, cloud_state):
            cloud_state(fails=True)

            await verified.async_refresh()

            assert verified.last_update_success is False
            assert verified.update_interval == timedelta(minutes=30)

        async def test_a_passive_sighting_gets_no_grace(self, verified, cloud_state):
            verified.ble = BleData(rssi=-60, connectable=False)
            cloud_state(fails=True)

            await verified.async_refresh()

            assert verified.last_update_success is False

        async def test_an_online_gateway_grants_grace_without_ble(
            self, hass, verified, cloud_state, api
        ):
            gw_coord = GatewaysUpdateCoordinator(
                hass, verified.config_entry, api, LockStateStore(hass)
            )
            gw_coord.last_update_success = True
            gw_coord.data = {
                1: Gateway(
                    gatewayId=1,
                    gatewayMac="00:00:00:00:00:01",
                    gatewayName="Front Gateway",
                    isOnline=True,
                )
            }
            hass.data.setdefault(DOMAIN, {})[verified.config_entry.entry_id] = {
                TT_GATEWAYS: gw_coord
            }

            # Fallback when data.gateways is empty uses any online gateway in gw_coord
            assert verified.gateway_online is True

            # Explicit link in data.gateways checks the linked gateway's online status
            verified.data.gateways = [
                GatewayLink(
                    gatewayId=1,
                    gatewayName="Front Gateway",
                    gatewayMac="00:00:00:00:00:01",
                    rssi=-68,
                )
            ]
            assert verified.gateway_online is True

            cloud_state(fails=True)
            await verified.async_refresh()
            assert verified.last_update_success is True
            assert verified.update_interval == RETRY_INTERVAL

            # Webhook state update resets reverify failures and restores poll interval
            verified._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_LOCK_10AM_UTC)
            )
            assert verified._reverify_failures == 0
            assert verified.update_interval == verified._poll_interval

            # Unsuccessful gw_coord or non-gateway lock returns gateway_online False
            gw_coord.last_update_success = False
            assert verified.gateway_online is False
            verified.has_gateway = False
            assert verified.gateway_online is False

    class TestLocalRefresh:
        """Between polls, a lock only the radio can reach is asked again.

        Asking costs the lock's battery, so every test here is about not
        asking when it buys nothing.
        """

        @staticmethod
        def _gatewayless(hass, api, options=None) -> LockUpdateCoordinator:
            config_entry = MockConfigEntry(domain=DOMAIN, options=options or {})
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=1,
                lockAlias="No Gateway",
                lockMac="00:00:00:00:00:00",
                hasGateway=0,
                userType="110301",
            )
            return LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )

        @pytest.fixture
        def gatewayless(self, hass, api) -> LockUpdateCoordinator:
            return self._gatewayless(hass, api)

        @pytest.fixture
        def heard(self, gatewayless) -> LockUpdateCoordinator:
            gatewayless.ble = BleData(rssi=-60, connectable=True)
            gatewayless.data.locked = False
            return gatewayless

        @pytest.fixture
        def radio(self, monkeypatch):
            """Script what the radio answers; returns the setter."""

            def _answers(status: LockStatus | None) -> None:
                async def async_read_state_ble(self):
                    return status

                monkeypatch.setattr(
                    LockUpdateCoordinator, "async_read_state_ble", async_read_state_ble
                )

            return _answers

        @pytest.fixture
        def radio_untouched(self, monkeypatch):
            async def async_read_state_ble(self):
                raise AssertionError("the radio should not have been used")

            monkeypatch.setattr(
                LockUpdateCoordinator, "async_read_state_ble", async_read_state_ble
            )

        async def test_a_cloud_reachable_lock_is_left_alone(
            self, coordinator, radio_untouched
        ):
            """It already has the webhook, which is faster and free."""
            coordinator.ble = BleData(rssi=-60, connectable=True)

            await coordinator._async_local_refresh(dt_util.utcnow())

        async def test_a_lock_out_of_range_is_not_dialled(
            self, gatewayless, radio_untouched
        ):
            await gatewayless._async_local_refresh(dt_util.utcnow())

        async def test_a_lock_with_bluetooth_turned_off_is_not_dialled(
            self, hass, api, radio_untouched
        ):
            lock = self._gatewayless(hass, api, {CONF_BLUETOOTH_ENABLED: False})
            lock.ble = BleData(rssi=-60, connectable=True)

            await lock._async_local_refresh(dt_util.utcnow())

        async def test_a_bolt_that_moved_is_published(self, heard, radio):
            heard.data.sensor = SensorData(battery=50)
            radio(LockStatus(battery=87, locked=True, door_open=False))
            notified = Mock()
            heard.async_update_listeners = notified

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.locked is True
            assert heard.data.battery_level == 87
            assert heard.data.sensor.opened is False
            assert notified.call_count == 1

        async def test_a_lock_without_a_door_gets_no_door_state(self, heard, radio):
            """No door byte in the reply, so there is nothing to publish."""
            heard.data.sensor = SensorData()
            radio(LockStatus(battery=87, locked=True, door_open=None))

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.locked is True
            assert heard.data.sensor.opened is None

        async def test_a_door_reported_by_the_lock_needs_no_accessory(
            self, heard, radio
        ):
            """Only a lock with a door to report sends the byte, and a door
            built into the lock has no accessory for doorSensor/query to find -
            so the reading is the whole evidence of presence.
            """
            heard.data.sensor = SensorData()
            radio(LockStatus(battery=87, locked=True, door_open=True))

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.sensor.opened is True
            assert door_state_known(heard.data.sensor)

        async def test_a_lock_that_cannot_have_a_door_is_left_alone(self, heard, radio):
            """No door-sensor feature bit, so there is no sensor to fill in."""
            heard.data.sensor = None
            radio(LockStatus(battery=87, locked=True, door_open=True))

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.sensor is None

        async def test_publishing_does_not_reschedule_the_poll(self, heard, radio):
            """`async_set_updated_data` would restart the poll timer; at this
            cadence the poll, and the lock/detail read with it, would never
            come round again."""
            radio(LockStatus(battery=87, locked=True, door_open=None))
            set_updated = Mock()
            heard.async_set_updated_data = set_updated

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.locked is True
            assert set_updated.call_count == 0

        async def test_a_failed_read_publishes_nothing_and_spends_no_grace(
            self, heard, radio
        ):
            radio(None)
            notified = Mock()
            heard.async_update_listeners = notified

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.locked is False
            assert heard._reverify_failures == 0
            assert notified.call_count == 0

        async def test_a_reply_without_a_bolt_publishes_nothing(self, heard, radio):
            radio(LockStatus(battery=None, locked=None, door_open=None))
            notified = Mock()
            heard.async_update_listeners = notified

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard.data.locked is False
            assert notified.call_count == 0

        async def test_a_bolt_that_did_not_move_notifies_nobody(self, heard, radio):
            heard.data.locked = True
            heard.data.battery_level = 87
            radio(LockStatus(battery=87, locked=True, door_open=None))
            notified = Mock()
            heard.async_update_listeners = notified

            await heard._async_local_refresh(dt_util.utcnow())

            assert notified.call_count == 0

        async def test_a_successful_read_ends_the_grace_period(self, heard, radio):
            heard._reverify_failures = 2
            heard.update_interval = RETRY_INTERVAL
            radio(LockStatus(battery=87, locked=True, door_open=None))

            await heard._async_local_refresh(dt_util.utcnow())

            assert heard._reverify_failures == 0
            assert heard.update_interval == heard._poll_interval

        async def test_the_timer_fires_on_schedule(self, hass, heard, radio):
            radio(LockStatus(battery=87, locked=True, door_open=None))
            unsubscribe = heard.async_start_local_refresh()

            async_fire_time_changed(hass, dt_util.utcnow() + LOCAL_REFRESH_INTERVAL)
            await hass.async_block_till_done()

            assert heard.data.locked is True
            unsubscribe()

        async def test_the_timer_is_unsubscribable(self, hass, heard, radio_untouched):
            unsubscribe = heard.async_start_local_refresh()
            unsubscribe()

            async_fire_time_changed(hass, dt_util.utcnow() + LOCAL_REFRESH_INTERVAL)
            await hass.async_block_till_done()

    class TestGatewayLinks:
        """gateway/listByLock results feed LockState.gateways/best_gateway,
        and device_info.via_device_id (see coordinator.py's device_info
        docstring for why this matters)."""

        @pytest.mark.parametrize("with_gateway", [False, True])
        async def test_refresh_accepts_device_info_without_pop(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
            with_gateway,
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()

            info = dict(coordinator.device_info)
            assert coordinator.config_entry is not None
            registry = dr.async_get(hass)
            gateway = registry.async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                identifiers={(DOMAIN, "00:00:00:00:00:02")},
                name="Strong",
            )
            if with_gateway:
                info["via_device_id"] = gateway.id
            monkeypatch.setattr(
                LockUpdateCoordinator,
                "device_info",
                PropertyMock(return_value=MappingProxyType(info)),
            )

            await coordinator.async_refresh()

            device = registry.async_get_device_by_identifier(
                (DOMAIN, coordinator.data.mac), coordinator.config_entry.entry_id
            )
            assert device is not None
            assert device.name == info["name"]
            assert device.model == info["model"]
            assert device.via_device_id == (gateway.id if with_gateway else None)
            assert ("via_device_id" in info) == with_gateway

        async def test_best_gateway_by_rssi_and_via_device_id(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")
            assert coordinator.config_entry is not None
            gateway = dr.async_get(hass).async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                identifiers={(DOMAIN, "00:00:00:00:00:02")},
                name="Strong",
            )

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

            await coordinator.async_refresh()

            assert coordinator.data.best_gateway is not None
            assert coordinator.data.best_gateway.name == "Strong"
            assert coordinator.device_info["via_device_id"] == gateway.id

        async def test_refresh_links_lock_device_to_gateway_by_id(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            """_async_refresh_finished links the lock's device to the gateway
            via via_device_id (resolved ourselves), not the deprecated
            `via_device` identifier tuple removed in HA 2027.8.0."""
            mock_api_responses("default")

            assert coordinator.config_entry is not None
            registry = dr.async_get(hass)
            gateway = registry.async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                identifiers={(DOMAIN, "00:00:00:00:00:02")},
                name="Strong",
            )
            lookup = Mock(wraps=registry.async_get_device_by_identifier)
            monkeypatch.setattr(registry, "async_get_device_by_identifier", lookup)
            monkeypatch.setattr(
                registry,
                "async_get_device",
                Mock(side_effect=AssertionError("Deprecated lookup called")),
            )

            async def mock_get_gateways_for_lock(*args, **kwargs):
                return [
                    GatewayLink(
                        gatewayId=2,
                        gatewayName="Strong",
                        gatewayMac="00:00:00:00:00:02",
                        rssi=-60,
                    ),
                ]

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_gateways_for_lock",
                mock_get_gateways_for_lock,
            )

            await coordinator.async_refresh()

            lock_device = registry.async_get_device_by_identifier(
                (DOMAIN, coordinator.data.mac), coordinator.config_entry.entry_id
            )
            assert lock_device is not None
            assert lock_device.via_device_id == gateway.id
            lookup.assert_any_call(
                (DOMAIN, "00:00:00:00:00:02"), coordinator.config_entry.entry_id
            )

        @pytest.mark.parametrize("other_entry_gateway", [False, True])
        async def test_refresh_tolerates_unregistered_gateway(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
            other_entry_gateway,
        ):
            """If the best gateway isn't in the device registry yet, the lock
            device is still created, just without a via_device link."""
            mock_api_responses("default")
            assert coordinator.config_entry is not None
            registry = dr.async_get(hass)
            if other_entry_gateway:
                other_entry = MockConfigEntry(domain=DOMAIN)
                other_entry.add_to_hass(hass)
                registry.async_get_or_create(
                    config_entry_id=other_entry.entry_id,
                    identifiers={(DOMAIN, "00:00:00:00:00:02")},
                    name="Other account gateway",
                )
            lookup = Mock(wraps=registry.async_get_device_by_identifier)
            monkeypatch.setattr(registry, "async_get_device_by_identifier", lookup)
            monkeypatch.setattr(
                registry,
                "async_get_device",
                Mock(side_effect=AssertionError("Deprecated lookup called")),
            )

            async def mock_get_gateways_for_lock(*args, **kwargs):
                return [
                    GatewayLink(
                        gatewayId=2,
                        gatewayName="Strong",
                        gatewayMac="00:00:00:00:00:02",
                        rssi=-60,
                    ),
                ]

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_gateways_for_lock",
                mock_get_gateways_for_lock,
            )

            await coordinator.async_refresh()

            lock_device = registry.async_get_device_by_identifier(
                (DOMAIN, coordinator.data.mac), coordinator.config_entry.entry_id
            )
            assert lock_device is not None
            assert lock_device.via_device_id is None
            lookup.assert_any_call(
                (DOMAIN, "00:00:00:00:00:02"), coordinator.config_entry.entry_id
            )

        async def test_no_gateways_in_range_omits_via_device_id(
            self,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
        ):
            mock_api_responses("default")

            await coordinator.async_refresh()

            assert coordinator.data.best_gateway is None
            assert "via_device_id" not in coordinator.device_info

        async def test_wifi_only_lock_never_fetches_gateways(
            self, hass, api, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")

            called = False

            async def mock_get_gateways_for_lock(*args, **kwargs):
                nonlocal called
                called = True
                return []

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_gateways_for_lock",
                mock_get_gateways_for_lock,
            )

            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=1,
                lockAlias="WiFi Only",
                lockMac="00:00:00:00:00:01",
                hasGateway=0,
                featureValue=f"{2**56:X}",  # Features.wifi
                userType="110301",
            )
            wifi_coordinator = LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )

            await wifi_coordinator.async_refresh()

            assert wifi_coordinator.connectable is True
            assert called is False
            assert wifi_coordinator.data.gateways == []

    class TestUnifiViaDevice:
        """The lock links under the UniFi device that tracks its WiFi client.

        A true single-device merge is impossible in HA (there is no cross-entry
        device merge), so via_device (resolved to via_device_id) is the link.
        """

        def _make_wifi(self, coordinator: LockUpdateCoordinator) -> str:
            """Give the lock a WiFi feature + known MAC; return its WiFi MAC."""
            coordinator.data.mac = "16:72:4C:CC:01:C4"
            coordinator.data.features = Features.wifi
            return "16:72:4C:CC:01:C5"  # BLE MAC + 1, the tuple UniFi tracks

        async def test_via_device_set_when_unifi_device_exists(
            self, hass, coordinator: LockUpdateCoordinator
        ):
            wifi_mac = self._make_wifi(coordinator)
            assert coordinator.config_entry is not None
            other_entry = MockConfigEntry(domain="unifi")
            other_entry.add_to_hass(hass)
            unifi = dr.async_get(hass).async_get_or_create(
                config_entry_id=other_entry.entry_id,
                connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac))},
                name="UniFi client",
            )

            assert coordinator.device_info["via_device_id"] == unifi.id

        async def test_via_device_omitted_when_no_unifi_device(
            self, coordinator: LockUpdateCoordinator
        ):
            self._make_wifi(coordinator)

            assert "via_device_id" not in coordinator.device_info

        async def test_via_device_ignores_our_own_device(
            self, hass, coordinator: LockUpdateCoordinator
        ):
            """The ttlock device declares the same NETWORK_MAC, so a lookup can
            return it; it must be excluded by identifier, not self-linked."""
            wifi_mac = self._make_wifi(coordinator)
            assert coordinator.config_entry is not None
            dr.async_get(hass).async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                identifiers={(DOMAIN, coordinator.data.mac)},
                connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac))},
                name="The lock itself",
            )

            assert "via_device_id" not in coordinator.device_info

        async def test_registry_keeps_two_devices_and_links_via_device(
            self, hass, coordinator: LockUpdateCoordinator
        ):
            """What installed HA does: both devices declare the same WiFi MAC, so
            they stay SEPARATE (no cross-entry merge) and the lock links to the
            UniFi device through via_device_id."""
            wifi_mac = self._make_wifi(coordinator)
            assert coordinator.config_entry is not None
            registry = dr.async_get(hass)

            unifi_entry = MockConfigEntry(domain="unifi")
            unifi_entry.add_to_hass(hass)
            unifi = registry.async_get_or_create(
                config_entry_id=unifi_entry.entry_id,
                connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(wifi_mac))},
                name="UniFi client",
            )

            # Register the lock device exactly as the integration does.
            lock_device = registry.async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                **coordinator.device_info,
            )

            assert lock_device.id != unifi.id  # NOT merged into one device
            assert lock_device.via_device_id == unifi.id  # linked instead

            # The steady state on every poll/restart: BOTH devices are already
            # registered (the lock also declares the WiFi MAC). The link must
            # still resolve - a single connection lookup could return the lock
            # itself - and survive a re-register.
            assert coordinator.device_info.get("via_device_id") == unifi.id
            again = registry.async_get_or_create(
                config_entry_id=coordinator.config_entry.entry_id,
                **coordinator.device_info,
            )
            assert again.id == lock_device.id
            assert again.via_device_id == unifi.id

    class TestPollingCadence:
        """Lock state is re-verified every poll; slow-changing detail,
        passage-mode config and the gateway link are fetched at most once per
        slow interval (see coordinator.py _async_update_data). Cadence is
        driven by the entry's options, defaulting to 30 min / 6 h."""

        @staticmethod
        def _spy(monkeypatch, name):
            """Wrap the currently-installed mock for `name` in a call counter."""
            current = getattr(TTLockApi, name)
            spy = AsyncMock(side_effect=current)
            monkeypatch.setattr(f"custom_components.ttlock.api.TTLockApi.{name}", spy)
            return spy

        def _make_coordinator(self, hass, api, options=None):
            config_entry = MockConfigEntry(domain=DOMAIN, options=options or {})
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=7252408,
                lockAlias="Test Lock",
                lockMac="00:00:00:00:00:00",
                hasGateway=1,
                userType="110301",
            )
            return LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )

        async def test_defaults_when_no_options(self, hass, api):
            coordinator = self._make_coordinator(hass, api)
            assert coordinator.update_interval == timedelta(minutes=30)
            assert coordinator._slow_interval == timedelta(hours=6)

        async def test_intervals_from_options(self, hass, api):
            coordinator = self._make_coordinator(
                hass,
                api,
                options={CONF_POLL_INTERVAL: 45, CONF_SLOW_POLL_INTERVAL: 12},
            )
            assert coordinator.update_interval == timedelta(minutes=45)
            assert coordinator._slow_interval == timedelta(hours=12)

        async def test_state_reverified_every_poll_detail_fetched_once(
            self, coordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("with_sensor")
            get_lock = self._spy(monkeypatch, "get_lock")
            get_state = self._spy(monkeypatch, "get_lock_state")
            get_passage = self._spy(monkeypatch, "get_lock_passage_mode_config")
            get_gateways = self._spy(monkeypatch, "get_gateways_for_lock")

            await coordinator.async_refresh()
            await coordinator.async_refresh()
            await coordinator.async_refresh()

            # fast tier: every poll
            assert get_state.call_count == 3
            # slow tier: once, then gated
            assert get_lock.call_count == 1
            assert get_passage.call_count == 1
            assert get_gateways.call_count == 1

        async def test_concurrent_locks_share_one_ekey_refresh(
            self, hass, api, mock_api_responses, monkeypatch
        ):
            """One account-wide key/list request refreshes every lock in an entry."""
            mock_api_responses("default")
            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            store = LockStateStore(hass)

            def summary(lock_id: int, name: str) -> LockSummary:
                return LockSummary(
                    lockId=lock_id,
                    lockAlias=name,
                    lockMac=f"00:00:00:00:00:{lock_id:02x}",
                    hasGateway=1,
                    userType="110301",
                )

            coordinators = [
                LockUpdateCoordinator(
                    hass,
                    config_entry,
                    api,
                    summary(lock_id, "Before"),
                    LockTrafficCapture(),
                    store,
                )
                for lock_id in (1, 2)
            ]
            get_locks = AsyncMock(
                return_value=[summary(1, "Front Door"), summary(2, "Shed")]
            )
            monkeypatch.setattr(api, "get_locks", get_locks)

            await asyncio.gather(
                *(coordinator.async_refresh() for coordinator in coordinators)
            )

            get_locks.assert_awaited_once_with()
            assert [coordinator.summary.name for coordinator in coordinators] == [
                "Front Door",
                "Shed",
            ]

        async def test_slow_tier_refetched_after_interval(
            self, coordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            get_lock = self._spy(monkeypatch, "get_lock")
            get_passage = self._spy(monkeypatch, "get_lock_passage_mode_config")
            get_gateways = self._spy(monkeypatch, "get_gateways_for_lock")

            await coordinator.async_refresh()
            # simulate the slow interval having elapsed since the last fetch
            past = dt_util.now() - timedelta(hours=7)
            coordinator._details_last_fetched = past
            coordinator._passage_last_fetched = past
            coordinator._gateways_last_fetched = past

            await coordinator.async_refresh()

            assert get_lock.call_count == 2
            assert get_passage.call_count == 2
            assert get_gateways.call_count == 2

        async def test_detail_carried_forward_when_slow_tier_skipped(
            self, coordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            name = coordinator.data.name
            battery = coordinator.data.battery_level
            assert name is not None

            # second poll skips get_lock but must preserve prior detail
            await coordinator.async_refresh()
            assert coordinator.data.name == name
            assert coordinator.data.battery_level == battery

    class TestSensorAbsenceTracking:
        """See coordinator.py's _check_for_sensor - the whole reason issue
        #181 exists: TTLock's doorSensor/query can't distinguish "no sensor"
        from a transient failure, so we only give up after repeated
        failures, persist that verdict, and only re-probe once per restart
        afterwards.
        """

        LOCK_ID = 7252408

        def _make_coordinator(
            self, hass, api, store: LockStateStore
        ) -> LockUpdateCoordinator:
            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=self.LOCK_ID,
                lockAlias="Test Lock",
                lockMac="00:00:00:00:00:00",
                hasGateway=1,
                userType="110301",
            )
            return LockUpdateCoordinator(
                hass, config_entry, api, summary, LockTrafficCapture(), store
            )

        async def _expire_daily_gate(self, store: LockStateStore) -> None:
            await store.async_update(
                self.LOCK_ID,
                door_sensor_last_failed_req=(
                    dt_util.now() - timedelta(days=2)
                ).isoformat(),
            )

        async def test_does_not_recheck_within_the_same_day(
            self, hass, api, mock_api_responses
        ):
            mock_api_responses("sensor_not_installed")
            store = LockStateStore(hass)
            coordinator = self._make_coordinator(hass, api, store)

            await coordinator.async_refresh()
            await coordinator.async_refresh()

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_count_failed_req"] == 1

        async def test_confirms_absent_after_three_consecutive_failures(
            self, hass, api, mock_api_responses
        ):
            mock_api_responses("sensor_not_installed")
            store = LockStateStore(hass)
            coordinator = self._make_coordinator(hass, api, store)

            await coordinator.async_refresh()
            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_count_failed_req"] == 1
            assert entry.get("door_sensor_confirmed_absent", False) is False

            await self._expire_daily_gate(store)
            await coordinator.async_refresh()
            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_count_failed_req"] == 2
            assert entry.get("door_sensor_confirmed_absent", False) is False

            await self._expire_daily_gate(store)
            await coordinator.async_refresh()
            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_count_failed_req"] == 3
            assert entry["door_sensor_confirmed_absent"] is True

        async def test_no_extra_check_immediately_after_crossing_the_threshold(
            self, hass, api, mock_api_responses, monkeypatch
        ):
            """The refresh that crosses the failure threshold must itself
            count as this instance's one-shot recheck - otherwise the very
            next scheduled refresh (minutes later, not gated by the daily
            throttle that applied while still counting) would see
            confirmed_absent for "the first time" and fire one more
            unbudgeted call before finally going quiet.
            """
            mock_api_responses("sensor_not_installed")
            store = LockStateStore(hass)
            coordinator = self._make_coordinator(hass, api, store)

            await coordinator.async_refresh()
            await self._expire_daily_gate(store)
            await coordinator.async_refresh()
            await self._expire_daily_gate(store)
            await coordinator.async_refresh()

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_confirmed_absent"] is True

            call_count = 0

            async def counting_get_sensor(self, lock_id):
                nonlocal call_count
                call_count += 1

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_sensor",
                counting_get_sensor,
            )

            await coordinator.async_refresh()

            assert call_count == 0

        async def test_success_resets_failure_count(
            self, hass, api, mock_api_responses
        ):
            store = LockStateStore(hass)
            await store.async_update(self.LOCK_ID, door_sensor_count_failed_req=2)

            mock_api_responses("with_sensor")
            coordinator = self._make_coordinator(hass, api, store)
            await coordinator.async_refresh()

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_count_failed_req"] == 0
            assert entry["door_sensor_confirmed_absent"] is False

        async def test_new_instance_gets_one_recheck_after_confirmed_absent(
            self, hass, api, mock_api_responses, monkeypatch
        ):
            """Simulates an HA restart: a fresh LockUpdateCoordinator sharing
            the persisted store makes exactly one check, not the full
            three-strike sequence again.
            """
            mock_api_responses("sensor_not_installed")
            store = LockStateStore(hass)

            coordinator_a = self._make_coordinator(hass, api, store)
            await coordinator_a.async_refresh()
            await self._expire_daily_gate(store)
            await coordinator_a.async_refresh()
            await self._expire_daily_gate(store)
            await coordinator_a.async_refresh()

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_confirmed_absent"] is True

            call_count = 0

            async def counting_get_sensor(self, lock_id):
                nonlocal call_count
                call_count += 1

            monkeypatch.setattr(
                "custom_components.ttlock.api.TTLockApi.get_sensor",
                counting_get_sensor,
            )

            coordinator_b = self._make_coordinator(hass, api, store)
            await coordinator_b.async_refresh()
            await coordinator_b.async_refresh()
            await coordinator_b.async_refresh()

            assert call_count == 1

        async def test_confirmed_absent_recheck_success_clears_state(
            self, hass, api, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            store = LockStateStore(hass)
            await store.async_update(
                self.LOCK_ID,
                door_sensor_confirmed_absent=True,
                door_sensor_count_failed_req=3,
            )

            coordinator = self._make_coordinator(hass, api, store)
            await coordinator.async_refresh()

            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.present is True

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_confirmed_absent"] is False
            assert entry["door_sensor_count_failed_req"] == 0

        async def test_confirmed_absent_recheck_failure_leaves_state_unchanged(
            self, hass, api, mock_api_responses
        ):
            mock_api_responses("sensor_not_installed")
            store = LockStateStore(hass)
            await store.async_update(
                self.LOCK_ID,
                door_sensor_confirmed_absent=True,
                door_sensor_count_failed_req=3,
            )

            coordinator = self._make_coordinator(hass, api, store)
            await coordinator.async_refresh()

            entry = await store.async_get(self.LOCK_ID)
            assert entry["door_sensor_confirmed_absent"] is True
            assert entry["door_sensor_count_failed_req"] == 3

    class TestProcessWebhookData:
        async def test_lock_works(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = False

            event = WebhookEvent.model_validate(WEBHOOK_LOCK_10AM_UTC)

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "lock by lock key"

        async def test_unlock_works(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = -1
            event = WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "unlock by IC card"

        async def test_auto_lock_works(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_LOCKED)
            )
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)
            coordinator.data.passage_mode_config = PassageModeConfig.model_validate(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )

            event = WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)

            assert coordinator.data.auto_lock_delay(event.lock_ts) == 1

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "unlock by IC card"

            await asyncio.sleep(2)

            # the lock was asked, and said locked
            get_lock_state.assert_awaited_once()
            assert coordinator.data.locked is True
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "Auto Lock"

        async def test_auto_lock_asks_the_lock_instead_of_assuming(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None
            # the door is held open, or the bolt jammed: it never re-locked
            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_UNLOCKED)
            )
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)

            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            )
            await asyncio.sleep(1.5)

            get_lock_state.assert_awaited_once()
            assert coordinator.data.locked is False
            assert coordinator.data.last_reason == "unlock by IC card"

        async def test_auto_lock_failed_read_keeps_last_known_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None
            get_lock_state = AsyncMock(side_effect=RequestFailed("cloud is down"))
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)

            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            )
            await asyncio.sleep(1.5)

            get_lock_state.assert_awaited_once()
            assert coordinator.data.locked is False
            # one missed check doesn't make the lock unavailable
            assert coordinator.last_update_success is True

        async def test_auto_lock_unanswered_read_keeps_last_known_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None
            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_UNKNOWN)
            )
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)

            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            )
            await asyncio.sleep(1.5)

            get_lock_state.assert_awaited_once()
            assert coordinator.data.locked is False

        async def test_auto_lock_ignores_a_slow_lock_clock(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None
            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_LOCKED)
            )
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)

            # the lock's clock is a minute behind the server's
            event = WebhookEvent.model_validate(
                {
                    **WEBHOOK_UNLOCK_10AM_UTC,
                    "lockDate": 1682244437000,
                }
            )
            coordinator._process_webhook_data(event)
            await asyncio.sleep(0.5)

            # the countdown still runs its full length
            get_lock_state.assert_not_awaited()
            assert coordinator.data.locked is False

            await asyncio.sleep(1)
            get_lock_state.assert_awaited_once()
            assert coordinator.data.locked is True

        async def test_auto_lock_does_not_ask_an_unreachable_lock(self, hass, api):
            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=1,
                lockAlias="No Gateway",
                lockMac="00:00:00:00:00:01",
                hasGateway=0,
                userType="110301",
            )
            coordinator = LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )
            assert coordinator.connectable is False
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_LOCKED)
            )
            api.get_lock_state = get_lock_state

            coordinator._process_webhook_data(
                WebhookEvent.model_validate({**WEBHOOK_UNLOCK_10AM_UTC, "lockId": 1})
            )
            await asyncio.sleep(1.5)

            get_lock_state.assert_not_awaited()
            assert coordinator.data.locked is False
            assert coordinator.last_update_success is True

        async def test_open_works(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = -1
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.opened = False

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_OPEN)
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.opened is True

        async def test_close_does_not_assume_locked(
            self, hass, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            coordinator.data.locked = False
            coordinator.data.auto_lock_seconds = -1
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.opened = True

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_CLOSE)
            coordinator._process_webhook_data(event)

            # a closed door says nothing about the bolt
            assert coordinator.data.locked is False
            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.opened is False

            # the lock still reports unlocked when asked
            await hass.async_block_till_done()
            assert coordinator.data.locked is False

        async def test_close_does_not_read_the_lock(
            self,
            hass,
            coordinator: LockUpdateCoordinator,
            mock_api_responses,
            monkeypatch,
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            coordinator.data.locked = False
            coordinator.data.auto_lock_seconds = -1
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.opened = True

            get_lock_state = AsyncMock(
                return_value=WireLockState.model_validate(LOCK_STATE_LOCKED)
            )
            monkeypatch.setattr(coordinator.api, "get_lock_state", get_lock_state)

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_CLOSE)
            coordinator._process_webhook_data(event)
            await hass.async_block_till_done()

            # a door close costs no extra lock-state read
            get_lock_state.assert_not_awaited()
            assert coordinator.data.locked is False

        async def test_close_on_unreachable_lock_keeps_last_known_state(
            self, hass, api
        ):
            config_entry = MockConfigEntry(domain=DOMAIN)
            config_entry.add_to_hass(hass)
            summary = LockSummary(
                lockId=1,
                lockAlias="No Gateway",
                lockMac="00:00:00:00:00:01",
                hasGateway=0,
                userType="110301",
            )
            coordinator = LockUpdateCoordinator(
                hass,
                config_entry,
                api,
                summary,
                LockTrafficCapture(),
                LockStateStore(hass),
            )
            assert coordinator.connectable is False
            coordinator.data.locked = False
            coordinator.data.sensor = SensorData(opened=True, battery=90)

            event = WebhookEvent.model_validate({**WEBHOOK_SENSOR_CLOSE, "lockId": 1})
            coordinator._process_webhook_data(event)
            await hass.async_block_till_done()

            assert coordinator.data.locked is False
            assert coordinator.data.sensor.opened is False
            # no refresh was attempted, so the lock isn't marked unavailable
            assert coordinator.last_update_success is True

        async def test_second_unlock_restarts_auto_lock_timer(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 2
            coordinator.data.passage_mode_config = None
            monkeypatch.setattr(
                coordinator.api,
                "get_lock_state",
                AsyncMock(return_value=WireLockState.model_validate(LOCK_STATE_LOCKED)),
            )

            event = WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)

            coordinator._process_webhook_data(event)
            await asyncio.sleep(1)
            assert coordinator.data.locked is False

            # unlocked again before the first timer ran out
            coordinator._process_webhook_data(event)
            await asyncio.sleep(1.5)

            # the first unlock's timer would have fired by now
            assert coordinator.data.locked is False

            await asyncio.sleep(1)
            assert coordinator.data.locked is True
            assert coordinator.data.last_reason == "Auto Lock"

        async def test_shutdown_cancels_pending_auto_lock(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None

            event = WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            coordinator._process_webhook_data(event)

            await coordinator.async_shutdown()
            await asyncio.sleep(1.5)

            assert coordinator.data.locked is False

        async def test_lock_event_cancels_pending_auto_lock(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None

            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            )
            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_LOCK_10AM_UTC)
            )
            assert coordinator.data.locked is True

            # unlocked again by a path that sends no webhook
            coordinator.data.locked = False
            await asyncio.sleep(1.5)

            # the timer from before the lock doesn't fire
            assert coordinator.data.locked is False

        async def test_a_door_without_an_accessory_still_follows_events(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            """A door built into the lock has no accessory, but the events it
            reports are the same ones.
            """
            mock_api_responses("sensor_not_installed")
            await coordinator.async_refresh()
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.opened = False

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_OPEN)
            coordinator._process_webhook_data(event)

            assert coordinator.data.sensor.opened is True

        async def test_ignores_events_for_other_locks(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = False

            event = WebhookEvent.model_validate(
                {**WEBHOOK_LOCK_10AM_UTC, "lockId": coordinator.lock_id + 1}
            )
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False

        async def test_ignores_unsuccessful_events(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = False

            event = WebhookEvent.model_validate({**WEBHOOK_LOCK_10AM_UTC, "success": 0})
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False

    class TestAsyncUpdateDataErrors:
        async def test_a_slow_tier_failure_never_fails_the_poll(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            """Detail is not availability: a lock whose state reads stays usable."""
            mock_api_responses("default")
            get_lock = AsyncMock(side_effect=RequestFailed("boom"))
            monkeypatch.setattr(coordinator.api, "get_lock", get_lock)
            monkeypatch.setattr(
                coordinator.api,
                "get_lock_passage_mode_config",
                AsyncMock(side_effect=TimeoutError()),
            )
            monkeypatch.setattr(
                coordinator.api, "get_locks", AsyncMock(side_effect=TimeoutError())
            )

            await coordinator.async_refresh()
            await coordinator.async_refresh()

            assert coordinator.last_update_success is True
            assert coordinator.data.locked is False  # the fast tier still landed
            get_lock.assert_awaited_once()  # stamped on the failed attempt

        async def test_a_failed_state_read_still_fails_once_verified(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            monkeypatch.setattr(
                coordinator.api,
                "get_lock_state",
                AsyncMock(side_effect=RequestFailed("boom")),
            )

            with pytest.raises(UpdateFailed):
                await coordinator._async_update_data()

    class TestLockUnlock:
        async def test_lock_success_updates_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = False

            async def mock_lock(lock_id):
                # verify action_pending is set for the duration of the call
                assert coordinator.data.action_pending is True
                return True

            monkeypatch.setattr(coordinator.api, "lock", mock_lock)

            await coordinator.lock()

            assert coordinator.data.locked is True
            assert coordinator.data.action_pending is False

        async def test_lock_failure_leaves_state_unchanged(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = False
            monkeypatch.setattr(coordinator.api, "lock", AsyncMock(return_value=False))

            with pytest.raises(HomeAssistantError) as excinfo:
                await coordinator.lock()

            assert excinfo.value.translation_key == "lock_failed"
            assert coordinator.data.locked is False
            assert coordinator.data.action_pending is False

        async def test_unlock_success_updates_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            monkeypatch.setattr(coordinator.api, "unlock", AsyncMock(return_value=True))

            await coordinator.unlock()

            assert coordinator.data.locked is False
            assert coordinator.data.action_pending is False

        async def test_unlock_failure_leaves_state_unchanged(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            monkeypatch.setattr(
                coordinator.api, "unlock", AsyncMock(return_value=False)
            )

            with pytest.raises(HomeAssistantError) as excinfo:
                await coordinator.unlock()

            assert excinfo.value.translation_key == "unlock_failed"
            assert coordinator.data.locked is True
            assert coordinator.data.action_pending is False

    class TestSetAutoLock:
        async def test_turning_on_sets_default_delay(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            monkeypatch.setattr(
                coordinator.api, "set_auto_lock", AsyncMock(return_value=True)
            )

            await coordinator.set_auto_lock(True)

            assert coordinator.data.auto_lock_seconds == 10

        async def test_turning_off_clears_delay(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            monkeypatch.setattr(
                coordinator.api, "set_auto_lock", AsyncMock(return_value=True)
            )

            await coordinator.set_auto_lock(False)

            assert coordinator.data.auto_lock_seconds == 0

        async def test_turning_off_cancels_pending_auto_lock(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = None
            monkeypatch.setattr(
                coordinator.api, "set_auto_lock", AsyncMock(return_value=True)
            )

            coordinator._process_webhook_data(
                WebhookEvent.model_validate(WEBHOOK_UNLOCK_10AM_UTC)
            )
            pending = coordinator._auto_lock_task
            assert pending is not None

            await coordinator.set_auto_lock(False)
            await asyncio.sleep(0)

            assert coordinator._auto_lock_task is None
            assert pending.cancelled()

        async def test_failure_leaves_state_unchanged(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.auto_lock_seconds = 60
            monkeypatch.setattr(
                coordinator.api, "set_auto_lock", AsyncMock(return_value=False)
            )

            await coordinator.set_auto_lock(True)

            assert coordinator.data.auto_lock_seconds == 60

    class TestSetLockSound:
        async def test_turning_on_updates_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            monkeypatch.setattr(
                coordinator.api, "set_lock_sound", AsyncMock(return_value=True)
            )

            await coordinator.set_lock_sound(True)

            assert coordinator.data.lock_sound is True

        async def test_turning_off_updates_state(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            monkeypatch.setattr(
                coordinator.api, "set_lock_sound", AsyncMock(return_value=True)
            )

            await coordinator.set_lock_sound(False)

            assert coordinator.data.lock_sound is False

        async def test_failure_leaves_state_unchanged(
            self, coordinator: LockUpdateCoordinator, mock_api_responses, monkeypatch
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.lock_sound = True
            monkeypatch.setattr(
                coordinator.api, "set_lock_sound", AsyncMock(return_value=False)
            )

            await coordinator.set_lock_sound(False)

            assert coordinator.data.lock_sound is True


class TestGatewaysUpdateCoordinator:
    async def test_wraps_api_errors_in_update_failed(self, hass, api):
        config_entry = MockConfigEntry(domain=DOMAIN)
        gateways_coordinator = GatewaysUpdateCoordinator(
            hass, config_entry, api, LockStateStore(hass)
        )
        api.get_gateways = AsyncMock(side_effect=RequestFailed("boom"))

        with pytest.raises(UpdateFailed):
            await gateways_coordinator._async_update_data()


class TestBleFirstControl:
    """lock()/unlock() drive the bolt over Bluetooth first, cloud as fallback.

    The routing (option gate, reachability, credential check, fallback) is what
    these cover; the wire protocol itself is tested in test_ble_protocol.py and
    the session in test_ble.py, so async_control is patched out here.
    """

    def _coordinator(self, hass, api, monkeypatch, *, control=True, reachable=True):
        # The api fixture has no OAuth session; uid would read from the token.
        monkeypatch.setattr(TTLockApi, "uid", 7, raising=False)
        options = {CONF_BLUETOOTH_ENABLED: True, CONF_BLE_CONTROL: control}
        entry = MockConfigEntry(domain=DOMAIN, options=options)
        entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=7252408,
            lockAlias="Test Lock",
            lockMac="00:00:00:00:00:00",
            hasGateway=1,
            userType="110301",
        )
        coord = LockUpdateCoordinator(
            hass, entry, api, summary, LockTrafficCapture(), LockStateStore(hass)
        )
        # Credentials as a slow-tier detail fetch would have left them.
        coord._ble_version = MOCK_LOCK_VERSION
        coord._ble_key = MOCK_LOCK_AES_KEY
        coord._ble_unlock_key = "2345"
        coord._ble_lock_flag_pos = 0
        if reachable:
            coord.ble = BleData(rssi=-55, connectable=True)
        return coord

    async def test_unlock_prefers_bluetooth(
        self, hass, api, mock_bluetooth, monkeypatch
    ):
        coord = self._coordinator(hass, api, monkeypatch)
        coord.data.locked = True
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coord.api, "unlock", cloud)
        with patch(
            "custom_components.ttlock.coordinator.async_control",
            new=AsyncMock(return_value=None),
        ) as ble_control:
            await coord.unlock()

        ble_control.assert_awaited_once()
        assert ble_control.await_args is not None
        assert ble_control.await_args.kwargs["unlock"] is True
        cloud.assert_not_called()
        assert coord.data.locked is False

    async def test_lock_prefers_bluetooth(self, hass, api, mock_bluetooth, monkeypatch):
        coord = self._coordinator(hass, api, monkeypatch)
        coord.data.locked = False
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coord.api, "lock", cloud)
        with patch(
            "custom_components.ttlock.coordinator.async_control",
            new=AsyncMock(return_value=None),
        ) as ble_control:
            await coord.lock()

        assert ble_control.await_args is not None
        assert ble_control.await_args.kwargs["unlock"] is False
        cloud.assert_not_called()
        assert coord.data.locked is True

    async def test_a_bluetooth_failure_falls_back_to_the_cloud(
        self, hass, api, mock_bluetooth, monkeypatch
    ):
        coord = self._coordinator(hass, api, monkeypatch)
        coord.data.locked = True
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coord.api, "unlock", cloud)
        with patch(
            "custom_components.ttlock.coordinator.async_control",
            new=AsyncMock(side_effect=BleError("out of range mid-command")),
        ):
            await coord.unlock()

        cloud.assert_awaited_once()
        assert coord.data.locked is False

    async def test_option_off_uses_the_cloud(
        self, hass, api, mock_bluetooth, monkeypatch
    ):
        coord = self._coordinator(hass, api, monkeypatch, control=False)
        coord.data.locked = True
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coord.api, "unlock", cloud)
        with patch(
            "custom_components.ttlock.coordinator.async_control",
            new=AsyncMock(return_value=None),
        ) as ble_control:
            await coord.unlock()

        ble_control.assert_not_called()
        cloud.assert_awaited_once()
        assert coord.data.locked is False

    async def test_out_of_range_uses_the_cloud(
        self, hass, api, mock_bluetooth, monkeypatch
    ):
        coord = self._coordinator(hass, api, monkeypatch, reachable=False)
        coord.data.locked = True
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coord.api, "unlock", cloud)
        with patch(
            "custom_components.ttlock.coordinator.async_control",
            new=AsyncMock(return_value=None),
        ) as ble_control:
            await coord.unlock()

        ble_control.assert_not_called()
        cloud.assert_awaited_once()


class TestConnectable:
    async def test_cloud_off_takes_the_cloud_route_away(self, coordinator):
        assert coordinator.connectable is True  # gatewayed, cloud on
        coordinator._cloud_enabled_option = False
        assert coordinator.connectable is False

    async def test_a_wifi_local_session_makes_a_lock_connectable(
        self, coordinator, monkeypatch
    ):
        coordinator._cloud_enabled_option = False
        monkeypatch.setattr(
            LockUpdateCoordinator, "wifi_local_drivable", property(lambda self: True)
        )
        assert coordinator.connectable is True

    async def test_a_wifi_link_change_starts_and_stops_the_poll(
        self, hass, coordinator, monkeypatch
    ):
        coordinator._cloud_enabled_option = False
        coordinator.update_interval = None
        drivable = {"now": True}
        monkeypatch.setattr(
            LockUpdateCoordinator,
            "wifi_local_drivable",
            property(lambda self: drivable["now"]),
        )
        refresh = AsyncMock()
        monkeypatch.setattr(coordinator, "async_request_refresh", refresh)

        coordinator.async_wifi_local_changed()
        await hass.async_block_till_done()
        assert coordinator.update_interval == timedelta(minutes=30)
        refresh.assert_awaited_once()

        drivable["now"] = False
        coordinator.async_wifi_local_changed()
        assert coordinator.update_interval is None


class TestCloudOffGates:
    @pytest.mark.parametrize(
        ("setter", "cloud_call", "value"),
        [
            ("set_lock_sound", "set_lock_sound", True),
            ("set_auto_lock_seconds", "set_auto_lock", 30),
        ],
    )
    async def test_settings_never_fall_back_to_the_cloud_when_off(
        self, coordinator, monkeypatch, setter, cloud_call, value
    ):
        coordinator._cloud_enabled_option = False
        cloud = AsyncMock(return_value=True)
        monkeypatch.setattr(coordinator.api, cloud_call, cloud)

        with pytest.raises(NoLocalAdminTransport):
            await getattr(coordinator, setter)(value)

        cloud.assert_not_awaited()

    async def test_a_wifi_read_without_a_bolt_falls_through(
        self, coordinator, mock_api_responses, monkeypatch
    ):
        """A takeover answer that carries no bolt must not blank the state."""
        mock_api_responses("default")
        monkeypatch.setattr(
            coordinator, "async_read_state_ble", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(
            coordinator,
            "async_read_state_wifi_local",
            AsyncMock(return_value=(None, 50)),
        )

        await coordinator.async_refresh()

        assert coordinator.data.locked is False  # the cloud answered


class TestBleCredentialCaching:
    """BLE credentials persist across restarts so control survives a cloud outage.

    lock/detail is the only cloud source of these; caching them means a lock in
    Bluetooth range is driveable straight after a restart, and while the cloud
    is unreachable entirely.
    """

    async def test_persists_after_a_detail_fetch(self, coordinator):
        coordinator._ble_version = MOCK_LOCK_VERSION
        coordinator._ble_key = MOCK_LOCK_AES_KEY
        coordinator._ble_unlock_key = "2345"
        coordinator._ble_lock_flag_pos = 3

        await coordinator._persist_ble_credentials()

        stored = await coordinator._store.async_get(coordinator.lock_id)
        assert stored["ble_key"] == MOCK_LOCK_AES_KEY
        assert stored["ble_version"] == MOCK_LOCK_VERSION
        assert stored["ble_unlock_key"] == "2345"
        assert stored["ble_lock_flag_pos"] == 3

    async def test_restores_cached_credentials(self, coordinator):
        await coordinator._store.async_update(
            coordinator.lock_id,
            ble_version=MOCK_LOCK_VERSION,
            ble_key=MOCK_LOCK_AES_KEY,
            ble_unlock_key="2345",
            ble_lock_flag_pos=4,
        )
        coordinator._ble_key = None  # as it would be before any cloud fetch

        await coordinator.async_restore_lock_cache()

        assert coordinator._ble_key == MOCK_LOCK_AES_KEY
        assert coordinator._ble_version == MOCK_LOCK_VERSION
        assert coordinator._ble_unlock_key == "2345"
        assert coordinator._ble_lock_flag_pos == 4

    async def test_restore_does_not_clobber_live_credentials(self, coordinator):
        coordinator._ble_key = "live-key"
        await coordinator._store.async_update(coordinator.lock_id, ble_key="stale-key")

        await coordinator.async_restore_lock_cache()

        assert coordinator._ble_key == "live-key"

    async def test_restore_is_a_noop_with_nothing_cached(self, coordinator):
        coordinator._ble_key = None

        await coordinator.async_restore_lock_cache()

        assert coordinator._ble_key is None

    async def test_an_incomplete_detail_never_replaces_a_complete_set(
        self, coordinator, mock_api_responses, monkeypatch
    ):
        """The last complete credentials may be the only way left to drive it."""
        mock_api_responses("default")
        coordinator._ble_version = MOCK_LOCK_VERSION
        coordinator._ble_key = MOCK_LOCK_AES_KEY
        coordinator._ble_unlock_key = "2345"
        coordinator._ble_admin_pwd = "4242"
        partial = Lock.model_validate({**BASIC_LOCK_DETAILS, "lockKey": None})
        monkeypatch.setattr(
            coordinator.api, "get_lock", AsyncMock(return_value=partial)
        )

        await coordinator.async_refresh()

        assert coordinator._ble_unlock_key == "2345"
        assert coordinator._ble_admin_pwd == "4242"
        assert coordinator.data.model == partial.model  # the rest still applies

    async def test_detail_and_passage_survive_a_restart(
        self, hass, coordinator, mock_api_responses, api
    ):
        mock_api_responses("default")
        await coordinator.async_refresh()

        restarted = LockUpdateCoordinator(
            hass,
            coordinator.config_entry,
            api,
            coordinator.summary,
            LockTrafficCapture(),
            coordinator._store,
        )
        await restarted.async_restore_lock_cache()

        assert restarted._ble_key == MOCK_LOCK_AES_KEY
        assert restarted._ble_admin_pwd == BASIC_LOCK_DETAILS["adminPwd"]
        assert restarted.data.model == BASIC_LOCK_DETAILS["modelNum"]
        assert restarted.data.firmware_version == BASIC_LOCK_DETAILS["firmwareRevision"]
        assert restarted.data.auto_lock_seconds == 60
        assert restarted.data.lock_sound is False
        assert restarted.data.tamper_alert is True
        assert restarted.data.features == Features.from_feature_value("F44354CD5F3")
        assert restarted._timezone_raw_offset == BASIC_LOCK_DETAILS["timezoneRawOffset"]
        assert (
            restarted.data.passage_mode_config == coordinator.data.passage_mode_config
        )


class _FakeTakeoverProxy:
    """A wifi_proxy stand-in with one ready takeover session.

    Mirrors the slice of WifiProxy the coordinator's read/poll path touches, so
    takeover reads can be exercised without a real listener or socket.
    """

    def __init__(
        self,
        status: tuple[bool | None, int | None] | None,
        *,
        reconnecting: bool = False,
    ) -> None:
        self._status = status
        self.reconnecting = reconnecting

    def is_takeover_ready(self, lock_id: int) -> bool:
        return self._status is not None

    def is_reconnecting_client(self, lock_id: int) -> bool:
        # A lock with a status is a ready client; one without is only worth
        # waiting on when the test says it is mid-reconnect.
        return self.reconnecting or self._status is not None

    def session_mode(self, lock_id: int) -> str | None:
        return "takeover"

    async def async_status(self, lock_id: int) -> tuple[bool | None, int | None] | None:
        return self._status


class TestTakeoverReads:
    """cloud_enabled=False: state comes from wifi_local, never the cloud."""

    def _install_proxy(self, hass, proxy) -> None:
        hass.data.setdefault(DOMAIN, {})[TT_WIFI_PROXY] = proxy

    async def test_takeover_reads_state_from_wifi_local_not_cloud(
        self,
        hass,
        coordinator: LockUpdateCoordinator,
        mock_api_responses,
        monkeypatch,
    ):
        mock_api_responses("default")
        coordinator._cloud_enabled_option = False
        # No radio read lands, so the fast tier would otherwise reach the cloud.
        monkeypatch.setattr(
            coordinator, "async_read_state_ble", AsyncMock(return_value=None)
        )
        # The cloud real-time state read must NOT be used in takeover.
        cloud_state = AsyncMock(
            side_effect=AssertionError("cloud state read used in takeover")
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_state", cloud_state
        )
        # A ready takeover session reports LOCKED, battery 0x4D (77).
        self._install_proxy(hass, _FakeTakeoverProxy((True, 77)))

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.data.locked is True
        assert coordinator.data.battery_level == 77  # wifi battery wins
        cloud_state.assert_not_called()

    async def test_takeover_reads_through_a_reconnect_blip(
        self,
        hass,
        coordinator: LockUpdateCoordinator,
        mock_api_responses,
        monkeypatch,
    ):
        """A reconnecting client (no ready session yet) is still read, not skipped.

        is_takeover_ready is False mid-blip, but is_reconnecting_client is True, so
        the read path calls async_status (which waits for the session) instead of
        falling straight through to the cloud.
        """
        mock_api_responses("default")
        coordinator._cloud_enabled_option = False
        monkeypatch.setattr(
            coordinator, "async_read_state_ble", AsyncMock(return_value=None)
        )
        cloud_state = AsyncMock(
            side_effect=AssertionError("cloud state read used in takeover")
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_state", cloud_state
        )

        class _BlipProxy(_FakeTakeoverProxy):
            # No ready session right now, but a recent client whose async_status
            # waits out the blip and returns a reading.
            def is_takeover_ready(self, lock_id: int) -> bool:
                return False

            def is_reconnecting_client(self, lock_id: int) -> bool:
                return True

        self._install_proxy(hass, _BlipProxy((True, 77)))

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.data.locked is True
        assert coordinator.data.battery_level == 77  # read came from wifi_local
        cloud_state.assert_not_called()

    async def test_takeover_without_a_session_degrades_to_unknown(
        self,
        hass,
        coordinator: LockUpdateCoordinator,
        mock_api_responses,
        monkeypatch,
    ):
        """A reconnecting client not yet answering, cloud off: unknown, not a failure."""
        mock_api_responses("default")
        coordinator._cloud_enabled_option = False
        monkeypatch.setattr(
            coordinator, "async_read_state_ble", AsyncMock(return_value=None)
        )
        cloud_state = AsyncMock(
            side_effect=AssertionError("cloud state read used in takeover")
        )
        monkeypatch.setattr(
            "custom_components.ttlock.api.TTLockApi.get_lock_state", cloud_state
        )
        self._install_proxy(hass, _FakeTakeoverProxy(None, reconnecting=True))

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True  # setup not hard-failed
        assert coordinator.data.locked is None  # unknown
        cloud_state.assert_not_called()

    async def test_cloud_off_makes_no_cloud_calls(
        self,
        coordinator: LockUpdateCoordinator,
        mock_api_responses,
        monkeypatch,
    ):
        """With the cloud off the poll reads locally and asks the cloud nothing."""
        mock_api_responses("default")
        coordinator._cloud_enabled_option = False
        self._install_proxy(coordinator.hass, _FakeTakeoverProxy(status=(True, 77)))
        calls = AsyncMock(side_effect=AssertionError("cloud called"))
        for name in (
            "get_locks",
            "get_lock",
            "get_sensor",
            "get_lock_state",
            "get_lock_passage_mode_config",
            "get_gateways_for_lock",
            "list_passcodes",
            "list_cards",
            "list_fingerprints",
        ):
            monkeypatch.setattr(f"custom_components.ttlock.api.TTLockApi.{name}", calls)
        coordinator.data.features = Features.door_sensor

        await coordinator.async_refresh()

        assert coordinator.last_update_success is True
        assert coordinator.data.locked is True
        calls.assert_not_awaited()
        stored = await coordinator._store.async_get(coordinator.lock_id)
        assert "door_sensor_count_failed_req" not in stored

    async def test_cloud_off_failed_local_reads_go_unavailable_after_grace(
        self,
        coordinator: LockUpdateCoordinator,
        monkeypatch,
    ):
        """A verified lock whose local reads stop is not shown stale as fresh."""
        coordinator._cloud_enabled_option = False
        proxy = _FakeTakeoverProxy(status=(True, 77), reconnecting=True)
        self._install_proxy(coordinator.hass, proxy)
        await coordinator.async_refresh()
        assert coordinator.last_update_success is True

        proxy._status = None
        for _ in range(REVERIFY_GRACE):
            await coordinator.async_refresh()
            assert coordinator.last_update_success is True
        await coordinator.async_refresh()

        assert coordinator.last_update_success is False


class TestLocalCardNames:
    """A locally-added card carries its given name, but never its number.

    The BLE/WiFi 0x05 add returns no id and carries no name, so the coordinator
    keeps the name display-only under a synthetic negative id (as passcodes do).
    The card number is a secret and must not reach the name cache or state.
    """

    @staticmethod
    def _coordinator(hass, api) -> LockUpdateCoordinator:
        config_entry = MockConfigEntry(domain=DOMAIN)
        config_entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=1,
            lockAlias="Cards",
            lockMac="00:00:00:00:00:00",
            hasGateway=0,
            userType="110301",
        )
        coordinator = LockUpdateCoordinator(
            hass,
            config_entry,
            api,
            summary,
            LockTrafficCapture(),
            LockStateStore(hass),
        )
        coordinator._run_local_admin_op = AsyncMock()
        coordinator._persist_credential_names = AsyncMock()
        coordinator.async_update_listeners = Mock()
        return coordinator

    async def test_add_records_name_under_synthetic_id_without_the_number(
        self, hass, api
    ):
        coordinator = self._coordinator(hass, api)

        await coordinator.add_card_local("1234567890", name="Front door fob")

        cards = coordinator.credential_names.cards
        assert len(cards) == 1
        cid, info = next(iter(cards.items()))
        assert cid < 0  # synthetic negative id
        assert info.name == "Front door fob"
        # The card number appears nowhere in the stored credential state.
        assert "1234567890" not in repr(cards)

    async def test_add_without_name_records_nothing(self, hass, api):
        coordinator = self._coordinator(hass, api)
        await coordinator.add_card_local("1234567890")
        assert coordinator.credential_names.cards == {}

    async def test_clear_drops_local_names_keeps_cloud_names(self, hass, api):
        coordinator = self._coordinator(hass, api)
        coordinator.credential_names = CredentialNames(
            cards={
                5: CredentialInfo(name="Cloud card"),  # positive = cloud-sourced
                -1: CredentialInfo(name="Local fob"),  # negative = locally added
            }
        )

        await coordinator.clear_cards_local()

        assert set(coordinator.credential_names.cards) == {5}


class TestLocalPasscodeExtras:
    """COUNT/CIRCLE adds record a display name + type; MODIFY records nothing.

    As with cards, a local 0x03 write returns no id and carries no name, so the
    name is kept display-only under a synthetic negative id and the digits never
    reach the cache or state. MODIFY cannot locate the old entry (no code stored),
    so it leaves the cache untouched.
    """

    @staticmethod
    def _coordinator(hass, api) -> LockUpdateCoordinator:
        config_entry = MockConfigEntry(domain=DOMAIN)
        config_entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=1,
            lockAlias="Codes",
            lockMac="00:00:00:00:00:00",
            hasGateway=0,
            userType="110301",
        )
        coordinator = LockUpdateCoordinator(
            hass,
            config_entry,
            api,
            summary,
            LockTrafficCapture(),
            LockStateStore(hass),
        )
        coordinator._run_local_admin_op = AsyncMock()
        coordinator._persist_credential_names = AsyncMock()
        coordinator.async_update_listeners = Mock()
        return coordinator

    async def test_cyclic_add_records_name_and_type_without_digits(self, hass, api):
        coordinator = self._coordinator(hass, api)

        await coordinator.add_cyclic_passcode_local(
            "1234", name="Cleaner", start=datetime(2026, 1, 2, 14, 30), cycle_type=48
        )

        passcodes = coordinator.credential_names.passcodes
        assert len(passcodes) == 1
        cid, info = next(iter(passcodes.items()))
        assert cid < 0
        assert info.name == "Cleaner"
        assert info.type == "CYCLIC"
        assert "1234" not in repr(passcodes)

    async def test_onetime_add_records_count_type(self, hass, api):
        coordinator = self._coordinator(hass, api)

        await coordinator.add_onetime_passcode_local(
            "1234",
            name="Guest",
            start=datetime(2026, 1, 2, 3, 4),
            end=datetime(2026, 12, 31, 23, 59),
        )

        info = next(iter(coordinator.credential_names.passcodes.values()))
        assert info.type == "COUNT"

    async def test_modify_records_no_name(self, hass, api):
        coordinator = self._coordinator(hass, api)

        await coordinator.modify_passcode_local("1234", "5678", pwd_type=1)

        assert coordinator.credential_names.passcodes == {}


class TestLocalFingerprintNames:
    """A locally-enrolled fingerprint carries its name, but never its number.

    Enrolment returns the number to the caller, but the coordinator keeps only the
    name, under a synthetic negative id (as cards/passcodes do). The number is a
    secret and must not reach the name cache or state.
    """

    @staticmethod
    def _coordinator(hass, api) -> LockUpdateCoordinator:
        config_entry = MockConfigEntry(domain=DOMAIN)
        config_entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=1,
            lockAlias="Fingerprints",
            lockMac="00:00:00:00:00:00",
            hasGateway=0,
            userType="110301",
        )
        coordinator = LockUpdateCoordinator(
            hass,
            config_entry,
            api,
            summary,
            LockTrafficCapture(),
            LockStateStore(hass),
        )
        coordinator._run_local_admin_op = AsyncMock()
        coordinator._persist_credential_names = AsyncMock()
        coordinator.async_update_listeners = Mock()
        return coordinator

    async def test_record_name_under_synthetic_id(self, hass, api):
        coordinator = self._coordinator(hass, api)

        await coordinator._record_local_name("fingerprints", "Right thumb")

        fingerprints = coordinator.credential_names.fingerprints
        assert len(fingerprints) == 1
        fid, info = next(iter(fingerprints.items()))
        assert fid < 0  # synthetic negative id
        assert info.name == "Right thumb"

    async def test_clear_drops_local_names_keeps_cloud_names(self, hass, api):
        coordinator = self._coordinator(hass, api)
        coordinator.credential_names = CredentialNames(
            fingerprints={
                5: CredentialInfo(name="Cloud print"),  # positive = cloud-sourced
                -1: CredentialInfo(name="Local print"),  # negative = locally enrolled
            }
        )

        await coordinator.clear_fingerprints_local()

        assert set(coordinator.credential_names.fingerprints) == {5}


class TestLocalSwitchAndPassage:
    """The 0x68 switch and 0x66 passage ops route through _run_local_admin_op."""

    @staticmethod
    def _coordinator(hass, api) -> LockUpdateCoordinator:
        config_entry = MockConfigEntry(domain=DOMAIN)
        config_entry.add_to_hass(hass)
        summary = LockSummary(
            lockId=1,
            lockAlias="Switches",
            lockMac="00:00:00:00:00:00",
            hasGateway=0,
            userType="110301",
        )
        return LockUpdateCoordinator(
            hass,
            config_entry,
            api,
            summary,
            LockTrafficCapture(),
            LockStateStore(hass),
        )

    async def test_set_tamper_alert_routes_and_updates_state(self, hass, api):
        coordinator = self._coordinator(hass, api)
        run = AsyncMock()
        update = Mock()
        coordinator._run_local_admin_op = run
        coordinator.async_update_listeners = update

        await coordinator.set_tamper_alert(True)

        run.assert_awaited_once()
        call = run.await_args
        assert call is not None
        assert call.args[0] == "set tamper alert"
        assert coordinator.data.tamper_alert is True
        update.assert_called_once()

    async def test_set_passage_auto_unlock_routes_and_updates_state(self, hass, api):
        coordinator = self._coordinator(hass, api)
        run = AsyncMock()
        coordinator._run_local_admin_op = run
        coordinator.async_update_listeners = Mock()

        await coordinator.set_passage_auto_unlock(False)

        run.assert_awaited_once()
        assert coordinator.data.passage_auto_unlock is False

    async def test_set_passage_mode_local_routes(self, hass, api):
        coordinator = self._coordinator(hass, api)
        run = AsyncMock()
        coordinator._run_local_admin_op = run
        coordinator.async_update_listeners = Mock()

        await coordinator.set_passage_mode_local(
            week_days=[1, 2],
            start_minute=60,
            end_minute=120,
            all_day=False,
            auto_unlock=True,
        )

        run.assert_awaited_once()
        call = run.await_args
        assert call is not None
        assert call.args[0] == "set passage mode"
        # auto_unlock given, so the cached switch state is updated too.
        assert coordinator.data.passage_auto_unlock is True
        # The cached passage config is kept in step for the binary sensor.
        config = coordinator.data.passage_mode_config
        assert config is not None
        assert bool(config.enabled) is True
        assert config.week_days == [1, 2]
        assert config.start_minute == 60

    async def test_clear_passage_mode_local_routes(self, hass, api):
        coordinator = self._coordinator(hass, api)
        run = AsyncMock()
        coordinator._run_local_admin_op = run
        coordinator.async_update_listeners = Mock()
        coordinator.data.passage_mode_config = PassageModeConfig.model_validate(
            {"passageMode": 1}
        )

        await coordinator.clear_passage_mode_local()

        call = run.await_args
        assert call is not None
        assert call.args[0] == "clear passage mode"
        config = coordinator.data.passage_mode_config
        assert config is not None
        assert bool(config.enabled) is False


class TestLogReadInBackground:
    """Cloud off: the local refresh fills the log cache over WiFi-local."""

    @staticmethod
    async def _wifi_lock(
        component_setup, mock_api_responses, monkeypatch, lock: LogLock
    ) -> tuple[LockUpdateCoordinator, MagicMock]:
        coordinator, _ = await _lock(component_setup, mock_api_responses)
        _cloud_off(monkeypatch)
        _log_locally(coordinator, monkeypatch, lock)
        proxy = coordinator.hass.data[DOMAIN][TT_WIFI_PROXY]
        proxy.is_takeover_ready.return_value = True
        coordinator.async_read_lock_status = AsyncMock(return_value=None)
        return coordinator, proxy

    @staticmethod
    async def _cached_ids(coordinator) -> list[int]:
        store = coordinator.hass.data[DOMAIN][TT_STORE]
        stored = (await store.async_get(coordinator.lock_id))["operate_log"]
        return [entry["record_id"] for entry in stored["records"]]

    async def test_refreshes_fill_the_cache_then_keep_it_current(
        self, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(250)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )

        for read in (100, 200, 250):
            await coordinator._async_local_refresh(dt_util.utcnow())
            assert await self._cached_ids(coordinator) == lock.ids[:read]
        log = coordinator._operate_log
        assert log is not None
        assert log.complete

        lock.calls.clear()
        await coordinator._async_local_refresh(dt_util.utcnow())
        assert lock.calls == [249, 250]  # nothing new: two commands

        lock.ids.append(0x20000000)
        await coordinator._async_local_refresh(dt_util.utcnow())
        assert await self._cached_ids(coordinator) == lock.ids

    async def test_no_background_read_with_the_cloud_on(
        self, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(5)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        monkeypatch.setattr(
            LockUpdateCoordinator, "cloud_enabled", property(lambda self: True)
        )

        await coordinator._async_background_log_read()

        assert lock.calls == []

    async def test_no_background_read_for_a_member_key(
        self, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(5)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        monkeypatch.setattr(
            LockUpdateCoordinator,
            "access_level",
            property(lambda self: AccessLevel.MEMBER),
        )

        await coordinator._async_local_refresh(dt_util.utcnow())

        assert lock.calls == []

    async def test_no_background_read_without_a_ready_wifi_session(
        self, component_setup, mock_api_responses, monkeypatch
    ):
        """Bluetooth alone is never dialled for the log: it costs the battery."""
        lock = LogLock(5)
        coordinator, proxy = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        proxy.is_takeover_ready.return_value = False
        ble = MagicMock(side_effect=AssertionError("Bluetooth was dialled"))
        monkeypatch.setattr(coord_mod, "async_admin_session", ble)
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
        )
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
        )

        await coordinator._async_local_refresh(dt_util.utcnow())

        assert lock.calls == []
        ble.assert_not_called()

    async def test_a_background_read_skips_bluetooth_first_in_the_order(
        self, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(5)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        ble = MagicMock(side_effect=AssertionError("Bluetooth was dialled"))
        monkeypatch.setattr(coord_mod, "async_admin_session", ble)
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_control_enabled", property(lambda self: True)
        )
        monkeypatch.setattr(
            LockUpdateCoordinator, "ble_reachable", property(lambda self: True)
        )
        coordinator._transport_order = [TRANSPORT_BLE, TRANSPORT_WIFI_LOCAL]

        await coordinator._async_local_refresh(dt_util.utcnow())

        ble.assert_not_called()
        assert await self._cached_ids(coordinator) == lock.ids

    async def test_a_lock_event_reads_the_log_once(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(5)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        await coordinator._async_background_log_read()
        coordinator.async_request_refresh = AsyncMock()
        coordinator._log_event_read.cooldown = 0.05
        lock.ids.append(0x20000000)
        lock.calls.clear()

        for _ in range(3):
            coordinator.async_wifi_local_event()
        await asyncio.sleep(0.2)
        await hass.async_block_till_done()

        assert lock.calls == [4, 5, 6]
        assert await self._cached_ids(coordinator) == lock.ids

    async def test_an_event_during_a_log_read_reads_again_after_it(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(5, delay=0.02)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        coordinator.async_request_refresh = AsyncMock()
        coordinator._log_event_read.cooldown = 0.05
        read = hass.async_create_task(coordinator._async_background_log_read())
        while len(lock.calls) < 3:
            await asyncio.sleep(0.005)

        coordinator.async_wifi_local_event()
        coordinator.async_wifi_local_event()
        await read
        lock.ids.append(0x20000000)  # written after the read passed the end
        lock.calls.clear()
        await asyncio.sleep(0.3)
        await hass.async_block_till_done()

        assert lock.calls == [4, 5, 6]  # one more read
        assert await self._cached_ids(coordinator) == lock.ids

    async def test_a_waiting_read_goes_before_the_next_page_of_a_background_read(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(20, delay=0.05)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        read = hass.async_create_task(coordinator._async_background_log_read())
        while len(lock.calls) < 3:
            await asyncio.sleep(0.01)
        before = len(lock.calls)
        pages: list[int] = []

        async def read_lock_config(_session):
            pages.append(len(lock.calls))
            return "config"

        assert (
            await coordinator.async_run_local_admin_op(
                "read lock config", read_lock_config, idempotent=True, read_only=True
            )
            == "config"
        )
        assert pages == [before]  # the page in flight, and no other
        await read
        # The background read resumed: each page once, one again, then the end.
        assert len(lock.calls) == 20 + 1 + 1
        assert await self._cached_ids(coordinator) == lock.ids

    async def test_a_background_read_interrupts_no_user_read(
        self, hass, component_setup, mock_api_responses, monkeypatch
    ):
        lock = LogLock(10, delay=0.02)
        coordinator, _ = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        user = hass.async_create_task(coordinator.async_read_operate_log())
        while not lock.calls:
            await asyncio.sleep(0.005)

        await coordinator._async_background_log_read()

        assert (await user).complete
        # The user read ran unbroken: pages 0-9 and the end, then the
        # background read's last page again and the end.
        assert lock.calls == [*range(11), 9, 10]

    async def test_a_failed_background_read_keeps_the_cache(
        self, component_setup, mock_api_responses, monkeypatch, caplog
    ):
        lock = LogLock(5)
        coordinator, proxy = await self._wifi_lock(
            component_setup, mock_api_responses, monkeypatch, lock
        )
        await coordinator._async_local_refresh(dt_util.utcnow())
        proxy.async_admin_op.side_effect = OSError()
        caplog.clear()

        await coordinator._async_local_refresh(dt_util.utcnow())

        proxy.async_admin_op.assert_awaited()
        assert await self._cached_ids(coordinator) == lock.ids
        assert coordinator.last_update_success
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
