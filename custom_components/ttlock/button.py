"""Button setup for TTLock diagnostics.

`StartDebugCapture` is sugar over HA's own logger hierarchy (see
docs/adr/0001-per-lock-debug-capture-via-logger-hierarchy.md) - it calls the
stock `logger.set_level` service against a lock's own child logger, then
schedules a callback to put the logger back where it found it after a fixed
window. It's not a separate capture pathway; power users can achieve the same
thing by hand via HA's Configure Logger UI.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.components.logger.const import (
    DOMAIN as LOGGER_DOMAIN,
    SERVICE_SET_LEVEL,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import get_device_logger
from .coordinator import LockUpdateCoordinator, lock_coordinators
from .entity import BaseLockEntity

CAPTURE_WINDOW = timedelta(minutes=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    async_add_entities(
        StartDebugCapture(coordinator) for coordinator in lock_coordinators(hass, entry)
    )


class StartDebugCapture(BaseLockEntity, ButtonEntity):
    """Raises a lock's own logger to debug for a fixed window, then reverts it."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: LockUpdateCoordinator) -> None:
        """Initialize with no capture in progress."""
        super().__init__(coordinator)
        self._prior_level: str | None = None
        self._cancel_revert: CALLBACK_TYPE | None = None

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Start Debug Capture"

    async def async_will_remove_from_hass(self) -> None:
        """Cancel any pending revert so it doesn't fire against a removed entity."""
        if self._cancel_revert is not None:
            self._cancel_revert()
            self._cancel_revert = None

    async def async_press(self) -> None:
        """Raise this lock's logger to debug, and (re)start the revert window."""
        logger = get_device_logger(self.coordinator.lock_id)

        if self._cancel_revert is None:
            self._prior_level = logging.getLevelName(logger.level)
        else:
            self._cancel_revert()

        await self._async_set_level(logger.name, "DEBUG")

        self._cancel_revert = async_call_later(
            self.hass, CAPTURE_WINDOW, self._async_revert
        )

    async def _async_revert(self, _now: datetime) -> None:
        """Put this lock's logger back to the level it was at before capture started."""
        logger = get_device_logger(self.coordinator.lock_id)
        level = self._prior_level or "NOTSET"
        self._cancel_revert = None
        self._prior_level = None

        await self._async_set_level(logger.name, level)

    async def _async_set_level(self, logger_name: str, level: str) -> None:
        """Call HA's built-in logger service to set one logger's level."""
        await self.hass.services.async_call(
            LOGGER_DOMAIN, SERVICE_SET_LEVEL, {logger_name: level}, blocking=True
        )
