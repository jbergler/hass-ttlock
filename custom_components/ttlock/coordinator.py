"""Provides the TTLock LockUpdateCoordinator."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt

from .api import TTLockApi
from .const import DOMAIN, SIGNAL_NEW_DATA, TT_LOCKS
from .models import Features, PassageModeConfig, State, WebhookEvent

_LOGGER = logging.getLogger(__name__)


@dataclass
class LockState:
    """Internal state of the lock as managed by the co-oridinator."""

    name: str
    mac: str
    model: str | None = None
    battery_level: int | None = None
    hardware_version: str | None = None
    firmware_version: str | None = None
    features: Features | None = None
    locked: bool | None = None
    action_pending: bool = False
    last_user: str | None = None
    last_reason: str | None = None

    auto_lock_seconds: int = -1
    passage_mode_config: PassageModeConfig | None = None

    def passage_mode_active(self, current_date: datetime = dt.now()) -> bool:
        """Check if passage mode is currently active."""
        if self.passage_mode_config and self.passage_mode_config.enabled:
            current_day = current_date.isoweekday()

            if current_day in self.passage_mode_config.week_days:
                if self.passage_mode_config.all_day:
                    return True

                current_minute = current_date.hour * 60 + current_date.minute
                if (
                    self.passage_mode_config.start_minute
                    <= current_minute
                    < self.passage_mode_config.end_minute
                ):
                    # Active by schedule
                    return True
        return False

    def auto_lock_delay(self, current_date: datetime) -> int | None:
        """Return the auto-lock delay in seconds, or None if auto-lock is currently disabled."""
        if self.auto_lock_seconds <= 0:
            return None

        if self.passage_mode_active(current_date):
            return None

        return self.auto_lock_seconds


@contextmanager
def lock_action(controller: LockUpdateCoordinator):
    """Wrap a lock action so that in-progress state is managed correctly."""

    controller.data.action_pending = True
    controller.async_update_listeners()
    try:
        yield
    finally:
        controller.data.action_pending = False
        controller.async_update_listeners()


def lock_coordinators(hass: HomeAssistant, entry: ConfigEntry):
    """Help with entity setup."""
    coordinators: list[LockUpdateCoordinator] = hass.data[DOMAIN][entry.entry_id][
        TT_LOCKS
    ]
    yield from coordinators


def coordinator_for(
    hass: HomeAssistant, entity_id: str
) -> LockUpdateCoordinator | None:
    """Given an entity_id, return the coordinator for that entity."""
    for entry in hass.config_entries.async_entries(DOMAIN):
        for coordinator in lock_coordinators(hass, entry):
            for entity in coordinator.entities:
                if entity.entity_id == entity_id:
                    return coordinator
    return None


class LockUpdateCoordinator(DataUpdateCoordinator[LockState]):
    """Class to manage fetching Toon data from single endpoint."""

    def __init__(self, hass: HomeAssistant, api: TTLockApi, lock_id: int) -> None:
        """Initialize the update co-ordinator for a single lock."""
        self.api = api
        self.lock_id = lock_id

        super().__init__(
            hass, _LOGGER, name=DOMAIN, update_interval=timedelta(minutes=15)
        )

        async_dispatcher_connect(self.hass, SIGNAL_NEW_DATA, self._process_webhook_data)

    async def _async_update_data(self) -> LockState:
        try:
            details = await self.api.get_lock(self.lock_id)

            new_data = deepcopy(self.data) or LockState(
                name=details.name,
                mac=details.mac,
                model=details.model,
                features=Features.from_feature_value(details.featureValue),
            )

            # update mutable attributes
            new_data.name = details.name
            new_data.battery_level = details.battery_level
            new_data.hardware_version = details.hardwareRevision
            new_data.firmware_version = details.firmwareRevision

            if new_data.locked is None:
                try:
                    state = await self.api.get_lock_state(self.lock_id)
                    new_data.locked = state.locked == State.locked
                except Exception:
                    pass

            new_data.auto_lock_seconds = details.autoLockTime
            new_data.passage_mode_config = await self.api.get_lock_passage_mode_config(
                self.lock_id
            )

            return new_data
        except Exception as err:
            raise UpdateFailed(err) from err

    @callback
    def _process_webhook_data(self, event: WebhookEvent):
        """Update data."""
        if event.id != self.lock_id:
            return

        _LOGGER.debug("Lock %s received %s", self.unique_id, event)

        if not event.success:
            return

        new_data = deepcopy(self.data)
        new_data.battery_level = event.battery_level

        if state := event.state:
            if state.locked == State.locked:
                new_data.locked = True
            elif state.locked == State.unlocked:
                new_data.locked = False
                self._handle_auto_lock(event.lock_ts, event.server_ts)

            if state.locked is not None:
                new_data.last_user = event.user
                new_data.last_reason = event.event.description

        self.async_set_updated_data(new_data)

    def _handle_auto_lock(self, lock_ts: datetime, server_ts: datetime):
        """Handle auto-locking the lock."""

        auto_lock_delay = self.data.auto_lock_delay(lock_ts)
        computed_msg_delay = max(0, (server_ts - lock_ts).total_seconds())

        if auto_lock_delay is None:
            _LOGGER.debug("Auto-lock is disabled")
            return

        async def _auto_locked(seconds: int, offset: float = 0):
            if seconds > 0 and (seconds - offset) > 0:
                await asyncio.sleep(seconds - offset)

            new_data = deepcopy(self.data)
            new_data.locked = True
            new_data.last_reason = "Auto Lock"

            _LOGGER.debug("Assuming lock auto locked after %s seconds", auto_lock_delay)
            self.async_set_updated_data(new_data)

        self.hass.create_task(_auto_locked(auto_lock_delay, computed_msg_delay))

    @property
    def unique_id(self) -> str:
        """Unique ID prefix for all entities for the lock."""
        return f"{DOMAIN}-{self.lock_id}"

    @property
    def device_info(self) -> DeviceInfo:
        """Device info for the lock."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.data.mac)},
            manufacturer="TT Lock",
            model=self.data.model,
            name=self.data.name,
            sw_version=self.data.firmware_version,
            hw_version=self.data.hardware_version,
        )

    @property
    def entities(self) -> list[Entity]:
        """Entities belonging to this co-ordinator."""
        return [
            callback.__self__
            for callback, _ in list(self._listeners.values())
            if isinstance(callback.__self__, Entity)
        ]

    def as_dict(self) -> dict:
        """Serialize for diagnostics."""
        return {
            "unique_id": self.unique_id,
            "device": asdict(self.data),
            "entities": [
                self.hass.states.get(entity.entity_id).as_dict()
                for entity in self.entities
            ],
        }

    async def lock(self) -> None:
        """Try to lock the lock."""
        with lock_action(self):
            res = await self.api.lock(self.lock_id)
            if res:
                self.data.locked = True

    async def unlock(self) -> None:
        """Try to unlock the lock."""
        with lock_action(self):
            res = await self.api.unlock(self.lock_id)
            if res:
                self.data.locked = False
