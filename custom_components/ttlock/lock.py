"""The actual lock part of the locks."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import lock_coordinators
from .entity import BaseLockEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    async_add_entities(
        [Lock(coordinator) for coordinator in lock_coordinators(hass, entry)]
    )


class Lock(BaseLockEntity, LockEntity):
    """The entity object for a lock."""

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = self.coordinator.data.name

        self._attr_is_locked = self.coordinator.data.locked
        self._attr_is_locking = (
            self.coordinator.data.action_pending and not self.coordinator.data.locked
        )
        self._attr_is_unlocking = (
            self.coordinator.data.action_pending and self.coordinator.data.locked
        )

    async def async_lock(self, **kwargs: Any) -> None:
        """Try to lock the lock."""
        await self.coordinator.lock()

    async def async_unlock(self, **kwargs: Any) -> None:
        """Try to unlock the lock."""
        await self.coordinator.unlock()
