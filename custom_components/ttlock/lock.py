"""Support for BMW car locks with BMW ConnectedDrive."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

import aiohttp

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_BATTERY_LEVEL, VOLUME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import PlatformNotReady
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import ApiLock, LockState
from .const import DOMAIN, TT_API, TT_LOCKS
from .entity import TTLockEntity

SCAN_INTERVAL = timedelta(hours=1)

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


class TTLock(LockEntity, TTLockEntity[ApiLock]):
    """The entity object for a lock."""

    _attr_code_format = None

    RECORD_TYPE_UNLOCK = [
        1,  # unlock by app
        4,  # unlock by passcode
        7,  # unlock by IC card
        8,  # unlock by fingerprint
        9,  # unlock by wrist strap
        10,  # unlock by Mechanical key
        12,  # unlock by gateway
        46,  # unlock by unlock key
        49,  # unlock by hotel card
        50,  # unlocked due to the high temperature
        55,  # unlock with key fob
        57,  # unlock with QR code success
        63,  # auto unlock at passage mode
    ]

    RECORD_TYPE_LOCK = [
        11,  # lock by app
        33,  # lock by fingerprint
        34,  # lock by passcode
        35,  # lock by IC card
        36,  # lock by Mechanical key
        45,  # Auto Lock
        47,  # lock by lock key
        61,  # Lock with QR code success
    ]

    # 29-apply some force on the Lock
    # 30-Door sensor closed
    # 31-Door sensor open
    # 32-open from inside
    # 37-Remote Control
    # 42-received new local mail
    # 43-received new other cities' mail
    # 44-Tamper alert
    # 48-System locked ( Caused by, for example: Using INVALID Passcode/Fingerprint/Card several times)
    # 51-Try to unlock with a deleted card
    # 52-Dead lock with APP
    # 53-Dead lock with passcode
    # 54-The car left (for parking lock)
    # 58-Unlock with QR code failed, it's expired
    # 59-Double locked
    # 60-Cancel double lock
    # 62-Lock with QR code failed, the lock is double locked

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

    def update_from_webhook(self, data: dict):
        """Process the webhook event to update lock state."""
        if data.get("success") == 1:
            # Only process lock state if the event indicated a successful operation
            event = data.get("recordType", -1)
            if event in self.RECORD_TYPE_UNLOCK:
                self._attr_is_locked = False
            elif event in self.RECORD_TYPE_LOCK:
                self._attr_is_locked = True

    @callback
    def _update_callback(self, data: dict):
        """Update data."""
        if self.device.id == data.get("lockId"):
            _LOGGER.debug(f"{data} is for this device ({self.entity_id})")
            self.update_from_webhook(data)
            self.async_write_ha_state()

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
