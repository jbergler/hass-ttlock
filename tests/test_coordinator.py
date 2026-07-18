"""Test ttlock setup process."""

import asyncio
from datetime import timedelta

import dateparser
import pytest

from custom_components.ttlock.coordinator import LockState, LockUpdateCoordinator
from custom_components.ttlock.models import PassageModeConfig, WebhookEvent
from homeassistant.util import dt as dt_util

from .const import (
    BASIC_LOCK_DETAILS,
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

            assert coordinator.data.sensor.opened is False
            assert coordinator.data.sensor.battery == 85
            assert coordinator.data.sensor.last_fetched > dt_util.now() - timedelta(
                seconds=3
            )

        async def test_coordinator_handles_missing_sensor(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("sensor_not_installed")
            await coordinator.async_refresh()

            assert coordinator.data.sensor.present is False
            assert coordinator.data.sensor.last_fetched > dt_util.now() - timedelta(
                seconds=3
            )

        async def test_sensor_data_only_fetched_once(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")

            await coordinator.async_refresh()
            t0 = coordinator.data.sensor.last_fetched

            await coordinator.async_refresh()
            t1 = coordinator.data.sensor.last_fetched

            assert t0 == t1

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
            coordinator.data.sensor.opened = False

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_OPEN)
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.sensor.opened is True

        async def test_close_works(
            self, coordinator: LockUpdateCoordinator, mock_api_responses
        ):
            mock_api_responses("with_sensor")
            await coordinator.async_refresh()

            coordinator.data.locked = False
            coordinator.data.auto_lock_seconds = -1
            coordinator.data.sensor.opened = True

            event = WebhookEvent.model_validate(WEBHOOK_SENSOR_CLOSE)
            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.sensor.opened is False
            assert coordinator.data.last_reason == "Door Closed"
