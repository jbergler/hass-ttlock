"""Support for BMW car locks with BMW ConnectedDrive."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_BATTERY_LEVEL, VOLUME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ApiLock, LockState
from .const import DOMAIN, TT_API, TT_LOCKS
from .entity import TTLockEntity

SCAN_INTERVAL = timedelta(seconds=60)

DOOR_LOCK_STATE = "door_lock_state"
_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""
    api = hass.data[DOMAIN][config_entry.entry_id][TT_API]

    entities: list[TTLock] = []
    try:
        locks = await api.get_locks()

        for lock in locks:
            _LOGGER.debug("Found lock %s" % (lock.name))
            entities.append(TTLock(api, lock))

        async_add_entities(entities, update_before_add=True)
        hass.data[DOMAIN][config_entry.entry_id][TT_LOCKS] = entities
    except aiohttp.ClientError as ex:
        raise PlatformNotReady from ex


class TTLock(TTLockEntity[ApiLock], LockEntity):
    """The entity object for a lock."""

    _attr_code_format = None

    @property
    def extra_device_info(self) -> dict[str, Any]:
        """Lock specific device info."""
        return {
            "sw_version": self.device.version,
        }

    def update_from_data(self) -> None:
        """Update the entity state from the latest data."""
        if self.device.state is None or self.device.state is LockState.unknown:
            self._attr_is_locked = None
        else:
            self._attr_is_locked = self.device.state == LockState.locked

        self._attr_extra_state_attributes = {
            ATTR_BATTERY_LEVEL: self.device.battery_level,
            VOLUME: self.device.volume if self.device.sound_enabled else 0,
        }

    async def async_lock(self, **kwargs: Any) -> None:
        """Lock the car."""
        _LOGGER.debug("%s: locking", self.device.name)
        if await self.device.lock():
            self.async_write_ha_state()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Unlock the car."""
        _LOGGER.debug("%s: unlocking", self.device.name)
        if await self.device.unlock():
            self.schedule_update_ha_state()
