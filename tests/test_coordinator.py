"""Test ttlock setup process."""


import asyncio

import dateparser
import pytest

from custom_components.ttlock.coordinator import LockState, LockUpdateCoordinator
from custom_components.ttlock.models import PassageModeConfig, WebhookEvent

from .const import (
    LOCK_DETAILS,
    PASSAGE_MODE_6_TO_6_7_DAYS,
    PASSAGE_MODE_ALL_DAY_WEEKDAYS,
    WEBHOOK_LOCK_10AM_UTC,
    WEBHOOK_UNLOCK_10AM_UTC,
)


async def test_coordinator_loads_data(
    coordinator: LockUpdateCoordinator, sane_default_data
):
    await coordinator.async_refresh()

    assert coordinator.data.name == LOCK_DETAILS["lockAlias"]
    assert coordinator.data.locked is False
    assert coordinator.data.action_pending is False
    assert coordinator.data.last_user is None
    assert coordinator.data.last_reason is None


@pytest.fixture(params=[{"auto_lock_seconds": 30}])
def lock_state(request):
    return LockState(
        name="Test Lock",
        mac="00:00:00:00:00:00",
        auto_lock_seconds=request.param["auto_lock_seconds"],
    )


def ts(time: str = "now"):
    return dateparser.parse(time)
    if ts:
        ts.replace(tzinfo=None)
    return ts


class TestLockState:
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
            lock_state.passage_mode_config = PassageModeConfig.parse_obj(
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
            lock_state.passage_mode_config = PassageModeConfig.parse_obj(
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
            lock_state.passage_mode_config = PassageModeConfig.parse_obj(
                PASSAGE_MODE_ALL_DAY_WEEKDAYS
            )
            assert lock_state.auto_lock_delay(ts(time)) is None


class TestLockUpdateCoordinator:
    class TestProcessWebhookData:
        async def test_lock_works(
            self, coordinator: LockUpdateCoordinator, sane_default_data
        ):
            await coordinator.async_refresh()
            coordinator.data.locked = False

            event = WebhookEvent.parse_obj(WEBHOOK_LOCK_10AM_UTC)

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is True
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "lock by lock key"

        async def test_unlock_works(
            self, coordinator: LockUpdateCoordinator, sane_default_data
        ):
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = -1
            event = WebhookEvent.parse_obj(WEBHOOK_UNLOCK_10AM_UTC)

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "unlock by IC card"

        async def test_auto_lock_works(
            self, hass, coordinator: LockUpdateCoordinator, sane_default_data
        ):
            await coordinator.async_refresh()
            coordinator.data.locked = True
            coordinator.data.auto_lock_seconds = 1
            coordinator.data.passage_mode_config = PassageModeConfig.parse_obj(
                PASSAGE_MODE_6_TO_6_7_DAYS
            )

            event = WebhookEvent.parse_obj(WEBHOOK_UNLOCK_10AM_UTC)

            assert coordinator.data.auto_lock_delay(event.lock_ts) == 1

            coordinator._process_webhook_data(event)

            assert coordinator.data.locked is False
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "unlock by IC card"

            await asyncio.sleep(2)

            assert coordinator.data.locked is True
            assert coordinator.data.last_user == "test"
            assert coordinator.data.last_reason == "Auto Lock"
