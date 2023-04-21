"""Support for iCloud sensors."""
from __future__ import annotations

import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .coordinator import lock_coordinators
from .entity import BaseLockEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up all the locks for the config entry."""

    async_add_entities(
        [
            entity
            for coordinator in lock_coordinators(hass, entry)
            for entity in [LockBattery(coordinator), LockOperator(coordinator)]
        ]
    )


class LockBattery(BaseLockEntity, SensorEntity):
    """Representation of a locks battery state."""

    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Battery"
        self._attr_native_value = self.coordinator.data.battery_level


class LockOperator(BaseLockEntity, RestoreEntity, SensorEntity):
    """Representation of a locks operator state."""

    # _attr_device_class = SensorDeviceClass.OPERATOR

    def _update_from_coordinator(self) -> None:
        """Fetch state from the device."""
        self._attr_name = f"{self.coordinator.data.name} Operator"
        if self.coordinator.data.last_user:
            self._attr_native_value = self.coordinator.data.last_user
        elif not self._attr_native_value:
            self._attr_native_value = "Unknown"

    async def async_added_to_hass(self) -> None:
        """Restore on startup since we don't have event history."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if not last_state or last_state.state == STATE_UNAVAILABLE:
            return

        self._attr_native_value = last_state.state
