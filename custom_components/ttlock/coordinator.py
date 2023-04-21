"""Provides the TTLock LockUpdateCoordinator."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TTLockApi
from .const import DOMAIN, SIGNAL_NEW_DATA, TT_API
from .models import State, WebhookEvent

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
    locked: bool | None = None
    action_pending: bool = False


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


class LockUpdateCoordinator(DataUpdateCoordinator[LockState]):
    """Class to manage fetching Toon data from single endpoint."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, lock_id: int) -> None:
        """Initialize the update co-ordinator for a single lock."""
        self.config_entry = entry
        self.api: TTLockApi = hass.data[DOMAIN][entry.entry_id][TT_API]
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
            )

            # update mutable attributes
            new_data.name = details.name
            new_data.battery_level = details.battery_level
            new_data.hardware_version = details.hardwareRevision
            new_data.firmware_version = details.firmwareRevision

            if new_data.locked is None:
                state = await self.api.get_lock_state(self.lock_id)
                new_data.locked = state.locked == State.locked

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

        self.async_set_updated_data(new_data)

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

    def as_dict(self) -> dict:
        """Serialize for diagnostics."""
        entities = [callback.__self__ for callback, _ in list(self._listeners.values())]
        return {
            "unique_id": self.unique_id,
            "device": asdict(self.data),
            "entities": [
                self.hass.states.get(entity.entity_id).as_dict()
                for entity in entities
                if isinstance(entity, Entity)
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
