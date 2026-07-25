"""Test ttlock setup process."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import dateparser
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ttlock.api import RequestFailed
from custom_components.ttlock.capture import LockTrafficCapture
from custom_components.ttlock.const import DOMAIN
from custom_components.ttlock.coordinator import (
    GatewaysUpdateCoordinator,
    LockState,
    LockUpdateCoordinator,
    SensorData,
    async_add_when_sensor_present,
)
from custom_components.ttlock.models import (
    LockState as WireLockState,
    LockSummary,
    PassageModeConfig,
    WebhookEvent,
)
from custom_components.ttlock.store import LockStateStore
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    BASIC_LOCK_DETAILS,
    LOCK_STATE_LOCKED,
    PASSAGE_MODE_6_TO_6_7_DAYS,
    PASSAGE_MODE_ALL_DAY_WEEKDAYS,
    WEBHOOK_LOCK_10AM_UTC,
    WEBHOOK_SENSOR_CLOSE,
    WEBHOOK_SENSOR_OPEN,
    WEBHOOK_UNLOCK_10AM_UTC,
)


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
            self, hass, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("default")
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
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

            assert coordinator.data.locked is True
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "Auto Lock"

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

        async def test_close_works(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            coordinator.data.locked = False
            coordinator.data.auto_lock_seconds = -1
            assert coordinator.data.sensor is not None
            coordinator.data.sensor.opened = True

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_CLOSE)
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.sensor is not None
            assert coordinator.data.sensor.opened is False
            assert coordinator.data.last_reason == "Door Closed"

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
        async def test_wraps_api_errors_in_update_failed(
            self, coordinator: LockUpdateCoordinator, monkeypatch
        ):
            monkeypatch.setattr(
                coordinator.api,
                "get_lock",
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

            await coordinator.lock()

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

            await coordinator.unlock()

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
        gateways_coordinator = GatewaysUpdateCoordinator(hass, config_entry, api)
        api.get_gateways = AsyncMock(side_effect=RequestFailed("boom"))

        with pytest.raises(UpdateFailed):
            await gateways_coordinator._async_update_data()
